#!/usr/bin/env python3
"""
Monitor de preços (GitHub Actions + Telegram)

Cada execução:
  1. DESCOBERTA (incremental): para alguns modelos por vez, busca o produto nas lojas
     configuradas, valida pelo código do fabricante e guarda os links em estado.json.
  2. CONSULTA: lê o preço de todos os links (fixos do config.json + descobertos).
     Camadas de acesso: direto (assinatura TLS de navegador) -> Chrome real
     (resolve desafios de JavaScript) -> Jina Reader (opcional).
     Loja que bloqueia 2 vezes seguidas é pulada no resto da execução.
  3. Grava historico.csv, calcula alertas e envia UMA mensagem compacta no Telegram.
"""
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:  # imita a conexão TLS de um navegador real
    from curl_cffi import requests as navegador
except ImportError:  # pragma: no cover
    navegador = None

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
HISTORICO = BASE / "historico.csv"
ESTADO = BASE / "estado.json"
CAMPOS = ["data_hora", "produto", "loja", "preco", "disponivel", "url"]
FUSO_BR = timezone(timedelta(hours=-3))
FORMATO_DATA = "%Y-%m-%d %H:%M"
LIMITE_TELEGRAM = 3900          # caracteres visíveis por mensagem (limite real: 4096)
FALHAS_PARA_DESISTIR = 2        # bloqueios seguidos antes de pular a loja
ESPERA = 1.0                    # multiplicador das pausas (os testes usam 0)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Seletores de reserva por domínio (usados só se JSON-LD e meta tags falharem)
SELETORES_LOJA = {
    "amazon.": ["#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                "#corePrice_feature_div .a-offscreen", "#price_inside_buybox",
                "#apex_desktop .a-price .a-offscreen"],
    "kabum.": ["h4.finalPrice", ".finalPrice"],
    "mercadolivre.": ['meta[itemprop="price"]', ".ui-pdp-price__second-line .andes-money-amount__fraction"],
    "pichau.": ['[data-cy="product-price"]'],
    "terabyteshop.": ["#valVista", ".val-prod"],
    "magazineluiza.": ['[data-testid="price-value"]'],
    "americanas.": ['[data-testid="price-value"]', ".priceSales"],
    "casasbahia.": ['[data-testid="product-price-value"]', "#product-price"],
    "carrefour.": [".vtex-product-price-1-x-sellingPriceValue"],
}

# Trechos de título que indicam página de bloqueio / desafio anti-robô
PAGINA_BLOQUEIO = ("robot check", "just a moment", "attention required", "access denied",
                   "captcha", "are you a human", "mercado libre", "acesso bloqueado",
                   "não é possível acessar", "nao e possivel acessar", "account verification",
                   "verificação de segurança", "pardon our interruption")

# Partes da página usadas para confirmar QUAL produto é (evita "produtos relacionados")
SELETORES_IDENTIDADE = ["title", "h1", "#productTitle", "#productDetails_techSpec_section_1",
                        "#productDetails_detailBullets_sections1", "#detailBullets_feature_div",
                        "#productOverview_feature_div", 'meta[name="description"]',
                        'meta[property="og:title"]']


def pausa(segundos):
    if ESPERA:
        time.sleep(segundos * ESPERA)


# ============================================================ utilidades
def parse_preco(valor):
    """'R$ 1.234,56' / '399,90' / '399.90' / 399.9 -> float. Zero ou vazio -> None."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if valor > 0 else None
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl_curto(v):
    return f"{round(v):,}".replace(",", ".")


def dominio(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def link(texto, url):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(texto)}</a>'


def texto_visivel(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def compacto(texto):
    """'MU-PC1T0T/AM  1 TB' -> 'mupc1t0tam1tb' (para comparar códigos e termos)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def agora_br():
    return datetime.now(FUSO_BR).strftime(FORMATO_DATA)


def _nos(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nos(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _nos(item)


def _jsonlds(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            yield json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue


def titulo_pagina(html_txt):
    m = re.search(r"<title[^>]*>(.*?)</title>", html_txt or "", re.I | re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def pagina_bloqueada(html_txt):
    t = titulo_pagina(html_txt).lower()
    if any(m in t for m in PAGINA_BLOQUEIO):
        return True
    # página vazia sem título nem dados de produto = desafio de JavaScript
    return not t and "application/ld+json" not in (html_txt or "") and len(html_txt or "") < 20000


# ============================================================ extração de preço
def precos_jsonld(soup):
    """Retorna ([(preco, disponivel)], esgotado_sem_preco)."""
    achados, esgotado_sem_preco = [], False
    for data in _jsonlds(soup):
        for no in _nos(data):
            tipo = no.get("@type")
            tipos = tipo if isinstance(tipo, list) else [tipo]
            eh_oferta = any(t in ("Offer", "AggregateOffer") for t in tipos) or (
                tipo is None and "price" in no and "availability" in no)
            if not eh_oferta:
                continue
            disp_txt = str(no.get("availability", "")).lower()
            disp = not any(x in disp_txt for x in ("outofstock", "soldout", "discontinued"))
            preco = parse_preco(no.get("price")) or parse_preco(no.get("lowPrice"))
            if preco:
                achados.append((preco, disp))
            elif not disp:
                esgotado_sem_preco = True
    return achados, esgotado_sem_preco


def precos_seletores(soup, seletores):
    for sel in seletores:
        achados = []
        for el in soup.select(sel):
            p = parse_preco(el.get("content") or el.get_text(" ", strip=True))
            if p:
                achados.append(p)
        if achados:
            return achados
    return []


META = ['meta[property="product:price:amount"]', 'meta[property="og:price:amount"]',
        'meta[itemprop="price"]', '[itemprop="price"]']


def extrair_preco(html_txt, url, faixa, seletor_custom=None):
    """(float, disp, metodo) | (None, False, 'esgotado') | (None, None, motivo)."""
    soup = BeautifulSoup(html_txt, "html.parser")
    lo, hi = faixa
    ok = lambda p: p is not None and lo <= p <= hi

    if seletor_custom:
        c = [p for p in precos_seletores(soup, [seletor_custom]) if ok(p)]
        if c:
            return c[0], True, "seletor"

    jsonld, esgotado_sem_preco = precos_jsonld(soup)
    c = [(p, d) for p, d in jsonld if ok(p)]
    if c:
        disponiveis = [x for x in c if x[1]]
        p, d = disponiveis[0] if disponiveis else c[0]
        return p, d, "json-ld"

    c = [p for p in precos_seletores(soup, META) if ok(p)]
    if c:
        return c[0], True, "meta"

    dom = dominio(url)
    for chave, sels in SELETORES_LOJA.items():
        if chave in dom:
            c = [p for p in precos_seletores(soup, sels) if ok(p)]
            if c:
                return c[0], True, "loja"

    if esgotado_sem_preco:
        return None, False, "esgotado"
    titulo = (soup.title.get_text(strip=True) if soup.title else "")[:60]
    if pagina_bloqueada(html_txt):
        return None, None, f"bloqueado (página '{titulo}')"
    return None, None, f"preço não encontrado (título: '{titulo}')"


# ============================================================ identidade do produto
def texto_identidade(html_txt):
    """Junta só as partes da página que descrevem O produto (título, ficha, JSON-LD do produto)."""
    soup = BeautifulSoup(html_txt, "html.parser")
    partes = []
    for sel in SELETORES_IDENTIDADE:
        for el in soup.select(sel)[:2]:
            partes.append(el.get("content") or el.get_text(" ", strip=True))
    for data in _jsonlds(soup):
        for no in _nos(data):
            tipo = no.get("@type")
            tipos = tipo if isinstance(tipo, list) else [tipo]
            if "Product" in tipos:
                for k in ("name", "sku", "mpn", "model", "gtin13", "gtin12", "description"):
                    if isinstance(no.get(k), (str, int)):
                        partes.append(str(no[k]))
                break
    titulo = " ".join(partes[:3])
    return " ".join(partes), titulo


# Regras globais: só SSD PORTÁTIL. Bloqueia SSD interno (M.2, SATA...) e acessórios (capas, estojos...)
_RE_INTERNO = re.compile(r"\bm[\s.\-_]?2\b|\bsata\b|\b2280\b|\binterno\b|\binternal\b|"
                         r"\bdissipador\b|\bheatsink\b|\bdesktop\b")
_RE_ACESSORIO = re.compile(
    r"\b(estojo|gaveta|enclosure|hub|dock)\b|"
    r"\b(capa|case|bolsa|suporte|cabo|adaptador|protetor|pelicula)\b[\s\-_]+(\w+[\s\-_]+){0,2}(para|p|compativel|for)\b")
_RE_PORTATIL = re.compile(r"\b(usb|extern[oa]s?|portat[ei]l|portateis|portable|external)\b")


def normal(texto):
    """Minúsculas sem acento, mantendo separadores (para regras por palavra)."""
    import unicodedata
    return unicodedata.normalize("NFKD", unquote(str(texto))).encode("ascii", "ignore").decode().lower()


def eh_interno(texto):
    return bool(_RE_INTERNO.search(normal(texto)))


def eh_acessorio(texto):
    return bool(_RE_ACESSORIO.search(normal(texto)))


def eh_portatil(texto):
    return bool(_RE_PORTATIL.search(normal(texto)))


def termos_ok(texto, produto):
    c = compacto(texto)
    return (all(compacto(t) in c for t in produto.get("obrigatorias", []))
            and not any(compacto(t) in c for t in produto.get("proibidas", [])))


def candidato_ok(trecho, produto):
    """Link de busca aceito se tem o código do fabricante ou os termos do modelo
    (nunca SSD interno nem acessório)."""
    if eh_interno(trecho) or eh_acessorio(trecho):
        return False
    c = compacto(trecho)
    if any(compacto(t) in c for t in produto.get("proibidas", [])):
        return False
    if any(compacto(cod) in c for cod in produto.get("codigos", [])):
        return True
    return termos_ok(trecho, produto)


def produto_confere(html_txt, produto):
    """True se a página é do modelo certo E de um SSD portátil:
    - título nunca pode indicar SSD interno (M.2, SATA...) nem acessório (capa, estojo...);
    - aceita pelo código do fabricante, ou pelos termos do modelo + palavra de portátil no título."""
    tudo, titulo = texto_identidade(html_txt)
    if eh_interno(titulo) or eh_acessorio(titulo):
        return False
    c = compacto(tudo)
    if any(compacto(cod) in c for cod in produto.get("codigos", [])):
        return True
    if produto.get("exigir_codigo"):     # linhas muito parecidas (ex.: SanDisk E30 x E31): só pelo código
        return False
    return bool(produto.get("obrigatorias")) and termos_ok(titulo, produto) and eh_portatil(titulo)


# ============================================================ busca nas lojas
def montar_url_busca(modelo_url, termos):
    return (modelo_url.replace("{mais}", quote_plus(termos))
                      .replace("{hifen}", quote(re.sub(r"\s+", "-", termos.strip().lower())))
                      .replace("{texto}", quote(termos)))


def canonico(url_relativa, base):
    url = urljoin(base, url_relativa)
    asin = re.search(r"/dp/([A-Z0-9]{10})", url)
    if asin and "amazon." in url:
        return f"https://{urlparse(url).netloc}/dp/{asin.group(1)}"
    return url.split("#")[0].split("?")[0]


def extrair_candidatos(html_txt, loja_cfg, produto, limite=3):
    """Acha links de produto na página de busca e filtra pelos termos do modelo."""
    bruto = html.unescape(html_txt or "").replace("\\u002F", "/").replace("\\/", "/")
    bruto = unquote(bruto)
    vistos, candidatos = set(), []
    for m in re.finditer(loja_cfg["padrao"], bruto, re.I):
        trecho = m.group(0)
        url = canonico(trecho, loja_cfg["base"])
        if url in vistos:
            continue
        vistos.add(url)
        if candidato_ok(trecho, produto):
            candidatos.append(url)
            if len(candidatos) >= limite:
                break
    return candidatos


# ============================================================ coleta
class Coletor:
    """Obtém páginas em camadas (direto -> Chrome real -> leitor) com controle de bloqueio por loja."""

    def __init__(self, usar_leitor=False, usar_navegador=False, dominios_navegador=None):
        self.usar_leitor = usar_leitor
        self.usar_navegador = usar_navegador
        self.dominios_navegador = dominios_navegador or ["*"]
        self.sessao = None
        self._pw = self._browser = self._contexto = None
        self.falhas_direto = Counter()
        self.falhas_total = Counter()
        self.cache = {}

    # ---- camada 1: acesso direto com assinatura de navegador
    def baixar(self, url, tentativas=2):
        erro = ""
        perfis = ["chrome", "safari"]
        for i in range(tentativas):
            try:
                if navegador:
                    if self.sessao is None or i > 0:
                        self.sessao = navegador.Session(impersonate=perfis[i % len(perfis)])
                    r = self.sessao.get(url, timeout=30,
                                        headers={"Accept-Language": HEADERS["Accept-Language"]})
                else:
                    r = requests.get(url, headers=HEADERS, timeout=25)
                if r.status_code == 200:
                    return r.text, None
                erro = f"HTTP {r.status_code}"
                if r.status_code == 404:
                    break
            except Exception as e:
                erro = str(e)[:100]
            pausa(2 + 3 * i)
        return None, erro

    # ---- camada 2: Chrome real (resolve desafios de JavaScript)
    def baixar_via_navegador(self, url):
        try:
            if self._contexto is None:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"])
                versao = self._browser.version.split(".")[0]
                self._contexto = self._browser.new_context(
                    locale="pt-BR", timezone_id="America/Sao_Paulo",
                    viewport={"width": 1366, "height": 768},
                    user_agent=(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                f"(KHTML, like Gecko) Chrome/{versao}.0.0.0 Safari/537.36"))
            pagina = self._contexto.new_page()
            try:
                resp = pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
                for _ in range(12):   # até ~24 s para o desafio se resolver
                    conteudo = pagina.content()
                    if not pagina_bloqueada(conteudo) and (
                            "application/ld+json" in conteudo or 'itemprop="price"' in conteudo
                            or len(conteudo) > 150000):
                        break
                    pagina.wait_for_timeout(2000)
                if resp is not None and resp.status == 404:
                    return None, "HTTP 404"
                return pagina.content(), None
            finally:
                pagina.close()
        except ImportError:
            self.usar_navegador = False
            return None, "playwright não instalado"
        except Exception as e:
            return None, str(e).splitlines()[0][:90]

    def fechar(self):
        for obj in (self._contexto, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    # ---- camada 3: leitor externo (Jina Reader)
    def baixar_via_leitor(self, url):
        cab = {"X-Return-Format": "html", "X-Timeout": "40", "Accept-Language": "pt-BR,pt;q=0.9"}
        if os.getenv("JINA_API_KEY"):
            cab["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        try:
            r = requests.get("https://r.jina.ai/" + url, headers=cab, timeout=60)
            if r.status_code == 200 and len(r.text) > 500:
                return r.text, None
            return None, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            return None, str(e)[:80]

    # ---- motor genérico
    def _usa_navegador(self, dom):
        return self.usar_navegador and any(d == "*" or d in dom for d in self.dominios_navegador)

    def bloqueada(self, url):
        return self.falhas_total[dominio(url)] >= FALHAS_PARA_DESISTIR

    def obter(self, url, aceitar):
        """Tenta as camadas até `aceitar(html)` devolver (True, resultado, metodo).
        Retorna (resultado|None, motivo). Só conta como bloqueio quando nenhuma camada
        conseguiu uma página utilizável (erro de rede, 403, desafio anti-robô)."""
        dom = dominio(url)
        if self.bloqueada(url):
            return None, "pulado (loja bloqueou nesta execução)"

        camadas = []
        if self.falhas_direto[dom] < FALHAS_PARA_DESISTIR:
            camadas.append(("direto", self.baixar))
        if self._usa_navegador(dom):
            camadas.append(("navegador", self.baixar_via_navegador))
        if self.usar_leitor:
            camadas.append(("leitor", self.baixar_via_leitor))

        motivos, so_bloqueios = [], True
        for nome, funcao in camadas:
            pagina, erro = funcao(url)
            if pagina and not pagina_bloqueada(pagina):
                ok, resultado, metodo = aceitar(pagina)
                if ok:
                    if nome == "direto":
                        self.falhas_direto[dom] = 0
                    self.falhas_total[dom] = 0
                    return resultado, (metodo if nome == "direto" else f"{metodo} pelo {nome}")
                so_bloqueios = False          # página normal, só sem o dado: tenta a próxima camada
                motivos.append(f"{nome}: {metodo}")
                continue
            motivo = erro or f"bloqueado (página '{titulo_pagina(pagina)[:50]}')"
            motivos.append(f"{nome}: {motivo}")
            if nome == "direto":
                self.falhas_direto[dom] += 1
            if erro == "HTTP 404":
                so_bloqueios = False
                break

        if so_bloqueios:
            self.falhas_total[dom] += 1
        return None, " | ".join(motivos)

    # ---- preço de uma página de produto
    def coletar(self, url, faixa, seletor=None, produto=None):
        if url in self.cache:
            return self.cache[url]

        def aceitar(pagina):
            if produto is not None and not produto_confere(pagina, produto):
                return False, None, "produto não confere com o modelo"
            p, d, m = extrair_preco(pagina, url, faixa, seletor)
            if p is not None or d is False:
                return True, (p, d), m
            return False, None, m

        resultado, metodo = self.obter(url, aceitar)
        saida = (resultado[0], resultado[1], metodo) if resultado else (None, None, metodo)
        self.cache[url] = saida
        return saida

    # ---- links de produto numa página de busca
    def buscar(self, url_busca, loja_cfg, produto, limite=3):
        def aceitar(pagina):
            c = extrair_candidatos(pagina, loja_cfg, produto, limite)
            return (True, c, f"{len(c)} candidato(s)") if c else (False, None, "nenhum produto compatível")
        resultado, motivo = self.obter(url_busca, aceitar)
        return resultado or [], motivo


# ============================================================ histórico e estado
def ler_historico(caminho=None):
    caminho = Path(caminho or HISTORICO)
    if not caminho.exists():
        return []
    with caminho.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def gravar_historico(linhas, caminho=None):
    caminho = Path(caminho or HISTORICO)
    novo = not caminho.exists()
    with caminho.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo:
            w.writeheader()
        w.writerows(linhas)


def reescrever_historico(linhas, caminho=None):
    caminho = Path(caminho or HISTORICO)
    with caminho.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas)


def aplicar_versao_regras(cfg, estado, historico):
    """Quando as regras de busca mudam (descoberta.versao), descarta os links descobertos
    antigos e apaga do histórico os preços desses links. Devolve o histórico limpo
    quantas linhas foram removidas e se houve troca de versão."""
    versao = cfg.get("descoberta", {}).get("versao", 1)
    if estado.get("versao_descoberta") == versao:
        return historico, 0, False
    fixas = {(o.get("url") or "").strip() for p in cfg["produtos"] for o in p.get("ofertas", [])}
    descartadas = {o["url"] for reg in estado.get("descobertos", {}).values()
                   for o in reg.get("ofertas", [])} - fixas
    estado["descobertos"] = {}
    estado["versao_descoberta"] = versao
    limpo = [h for h in historico if h.get("url") not in descartadas]
    return limpo, len(historico) - len(limpo), True


def ultimo_por_url(historico):
    ult = {}
    for h in historico:
        try:
            ult[h["url"]] = (float(h["preco"]), h["disponivel"] == "1")
        except (ValueError, KeyError):
            continue
    return ult


def ler_estado(caminho=None):
    caminho = Path(caminho or ESTADO)
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def gravar_estado(estado, caminho=None):
    Path(caminho or ESTADO).write_text(json.dumps(estado, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")


# ============================================================ descoberta automática
def lojas_para(produto, lojas_busca):
    marca = compacto(produto.get("marca", ""))
    for loja in lojas_busca:
        if loja.get("ativo") is False:
            continue
        marcas = [compacto(m) for m in loja.get("somente_marcas", [])]
        if marcas and marca not in marcas:
            continue
        yield loja


def modelos_a_descobrir(cfg, estado, agora):
    desc = cfg.get("descoberta", {})
    if not desc.get("ativa", True):
        return []
    validade = timedelta(hours=desc.get("validade_horas", 24))
    agora_dt = datetime.strptime(agora, FORMATO_DATA)
    registros = estado.get("descobertos", {})
    vencidos = []
    for p in cfg["produtos"]:
        if not p.get("busca"):
            continue
        reg = registros.get(p["nome"])
        quando = datetime.strptime(reg["atualizado"], FORMATO_DATA) if reg else datetime.min
        if agora_dt - quando >= validade:
            vencidos.append((quando, p))
    # Prioridade: nunca buscado primeiro; depois 500GB e os 1TB que formam par com eles
    # (precisam de preço para o comparativo); por último quem já tem links fixos no config.
    pares = {p.get("par_1tb") for p in cfg["produtos"] if p.get("par_1tb")}

    def prioridade(item):
        quando, p = item
        do_comparativo = (p.get("capacidade_gb") or 1000) < 1000 or p["nome"] in pares
        tem_fixas = any((o.get("url") or "").strip() and o.get("ativo") is not False for o in p.get("ofertas", []))
        return (quando, 0 if do_comparativo else 1, 1 if tem_fixas else 0)

    vencidos.sort(key=prioridade)
    return [p for _, p in vencidos[:desc.get("modelos_por_execucao", 2)]]


def descobrir(cfg, estado, coletor, agora):
    """Atualiza estado['descobertos'] para os modelos com descoberta vencida."""
    desc = cfg.get("descoberta", {})
    por_loja = desc.get("links_por_loja", 2)
    candidatos_max = desc.get("candidatos_por_loja", 3)
    registros = estado.setdefault("descobertos", {})
    for produto in modelos_a_descobrir(cfg, estado, agora):
        faixa = produto.get("faixa_valida", [1, 1_000_000])
        achados = []
        for loja in lojas_para(produto, cfg.get("lojas_busca", [])):
            url_busca = montar_url_busca(loja["busca"], produto["busca"])
            if coletor.bloqueada(url_busca):
                print(f"🔎 {produto['nome']} | {loja['loja']}: pulado (loja bloqueou nesta execução)")
                continue
            candidatos, motivo = coletor.buscar(url_busca, loja, produto, candidatos_max)
            validos = 0
            for url in candidatos:
                if validos >= por_loja:
                    break
                p, d, m = coletor.coletar(url, faixa, produto=produto)
                if p is not None or d is False:
                    achados.append({"loja": loja["loja"], "url": url})
                    validos += 1
                pausa(1)
            print(f"🔎 {produto['nome']} | {loja['loja']}: {validos} link(s) válido(s) ({motivo})")
            pausa(1)
        anterior = registros.get(produto["nome"], {}).get("ofertas", [])
        if not achados and anterior:
            achados = anterior   # busca falhou: mantém os links que já funcionavam
        registros[produto["nome"]] = {"atualizado": agora, "ofertas": achados}
    return estado


def ofertas_do_produto(produto, estado):
    """Links fixos do config + links descobertos (sem repetir URL)."""
    vistas, ofertas = set(), []
    for o in produto.get("ofertas", []):
        url = (o.get("url") or "").strip()
        if url.startswith("http") and o.get("ativo") is not False and url not in vistas:
            vistas.add(url)
            ofertas.append({**o, "url": url, "fixa": True})
    for o in estado.get("descobertos", {}).get(produto["nome"], {}).get("ofertas", []):
        if o["url"] not in vistas:
            vistas.add(o["url"])
            ofertas.append({**o, "fixa": False})
    return ofertas


# ============================================================ telegram
def telegram(texto, silencioso=False, botoes=None):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram não configurado — mensagem abaixo]\n" + texto_visivel(texto))
        for linha in botoes or []:
            print("  [botão] " + " | ".join(b["text"] for b in linha))
        return False
    dados = {"chat_id": chat, "text": texto, "parse_mode": "HTML",
             "disable_web_page_preview": "true", "disable_notification": "true" if silencioso else "false"}
    if botoes:
        dados["reply_markup"] = json.dumps({"inline_keyboard": botoes}, ensure_ascii=False)
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data=dados)
        if r.status_code != 200:
            print(f"Falha no Telegram: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Falha no Telegram: {e}")
        return False


def dividir_mensagem(blocos, limite=LIMITE_TELEGRAM):
    mensagens, atual = [], ""
    for b in blocos:
        candidato = f"{atual}\n\n{b}" if atual else b
        if atual and len(texto_visivel(candidato)) > limite:
            mensagens.append(atual)
            atual = b
        else:
            atual = candidato
    if atual:
        mensagens.append(atual)
    return mensagens


# ============================================================ mensagem
MEDALHAS = ["🥇", "🥈", "🥉"]


def marcador(i, estilo="medalhas"):
    """Posição no ranking: '🥇🥈🥉 4.' (medalhas) ou '1. 2. 3.' (numeros)."""
    if estilo == "numeros":
        return f"{i + 1}."
    return MEDALHAS[i] if i < len(MEDALHAS) else f"{i + 1}."


def rotulo_produto(prod):
    """Nome de exibição: acrescenta a capacidade quando ela foi informada e não está no nome."""
    cap = prod.get("capacidade_gb")
    if not cap:
        return prod["nome"]
    rot = rotulo_capacidade(cap)
    return prod["nome"] if compacto(rot) in compacto(prod["nome"]) else f"{prod['nome']} {rot}"


def rotulo_capacidade(gb):
    return f"{gb // 1000}TB" if gb >= 1000 and gb % 1000 == 0 else f"{gb}GB"


def ranking_modelos(resultados):
    """Melhor oferta disponível por loja, por modelo. Devolve (com_preco, sem_estoque)."""
    com_preco, sem_estoque = [], []
    for r in resultados:
        melhores = {}
        for o in r["ofertas"]:
            if o["preco"] is None or not o["disp"]:
                continue
            if o["loja"] not in melhores or o["preco"] < melhores[o["loja"]]["preco"]:
                melhores[o["loja"]] = o
        ofertas = sorted(melhores.values(), key=lambda o: o["preco"])
        item = {**r, "rotulo": r.get("rotulo", r["nome"]), "ofertas_ok": ofertas}
        if ofertas:
            com_preco.append(item)
        elif r["ofertas"]:
            sem_estoque.append(item)
    com_preco.sort(key=lambda x: x["ofertas_ok"][0]["preco"])
    return com_preco, sem_estoque


def secoes(com_preco, capacidades=None):
    """Agrupa por capacidade (maior primeiro). Sem capacidade informada = uma seção só.
    `capacidades` força seções mesmo sem preço (ex.: 500GB ainda sem leitura)."""
    caps = sorted({m.get("cap") or 0 for m in com_preco} | set(capacidades or []), reverse=True)
    return [(cap, [m for m in com_preco if (m.get("cap") or 0) == cap]) for cap in caps]


def custo_beneficio(com_preco, capacidades=None):
    """Compara cada 500GB com o 1TB do mesmo modelo e indica o melhor custo-benefício.
    Devolve (linhas_html, resumo_dict). Só atua quando há mais de uma capacidade
    (com preço, ou configurada em `capacidades`)."""
    modelos = [m for m in com_preco if m.get("cap")]
    if len({m["cap"] for m in modelos} | set(capacidades or [])) < 2:
        return [], {}
    if not modelos:
        return ["💡 <b>CUSTO-BENEFÍCIO</b>", "<i>Aguardando os primeiros preços para comparar.</i>"], {}
    por_nome = {m["nome"]: m for m in modelos}
    for m in modelos:
        m["preco"] = m["ofertas_ok"][0]["preco"]
        m["por_tb"] = m["preco"] / (m["cap"] / 1000)

    linhas = ["💡 <b>CUSTO-BENEFÍCIO</b> <i>(preço por TB)</i>"]
    pares = 0
    for m in sorted(modelos, key=lambda x: x["rotulo"]):
        par = por_nome.get(m.get("par") or "")
        if not par:
            continue
        pares += 1
        menor, maior = (m, par) if m["cap"] < par["cap"] else (par, m)
        razao = maior["por_tb"] / menor["por_tb"]
        dif = maior["preco"] - menor["preco"]
        aprox = "≈" if m.get("par_aproximado") else ""
        if razao <= 0.85:
            veredito = f"✅ {rotulo_capacidade(maior['cap'])} ({round((1 - razao) * 100)}% menos/TB)"
        elif razao >= 1:
            veredito = f"✅ {rotulo_capacidade(menor['cap'])} ({round((razao - 1) * 100)}% menos/TB)"
        else:
            veredito = f"⚖️ empate ({round((1 - razao) * 100)}% menos/TB no {rotulo_capacidade(maior['cap'])})"
        linhas.append(f"• {html.escape(menor['rotulo'])} {aprox}× {rotulo_capacidade(maior['cap'])}: "
                      f"{brl_curto(menor['preco'])} × {brl_curto(maior['preco'])} (+{brl_curto(dif)}) → {veredito}")
    if not pares:
        linhas.append("<i>Sem pares 500GB × 1TB com preço nesta consulta.</i>")

    melhor = min(modelos, key=lambda x: x["por_tb"])
    barato = min(modelos, key=lambda x: x["preco"])
    rapidos = [x for x in modelos if (x.get("vel") or 0) >= 2000]
    rapido = min(rapidos, key=lambda x: x["por_tb"]) if rapidos else None

    def oferta(x):
        o = x["ofertas_ok"][0]
        return f"{brl(o['preco'])} · {link(o['loja'], o['url'])}"

    vel = lambda x: (", " + f"{x['vel']:,}".replace(",", ".") + " MB/s") if x.get("vel") else ""
    linhas.append(f"🏆 Melhor custo/TB: <b>{html.escape(melhor['rotulo'])}</b> — "
                  f"R$ {brl_curto(melhor['por_tb'])}/TB ({oferta(melhor)}{vel(melhor)})")
    if barato is not melhor:
        extra = round((barato["por_tb"] / melhor["por_tb"] - 1) * 100)
        linhas.append(f"💰 Menor desembolso: <b>{html.escape(barato['rotulo'])}</b> — {oferta(barato)} "
                      f"<i>(paga {extra}% a mais por TB)</i>")
    if rapido and rapido is not melhor:
        linhas.append(f"⚡ Rápido (≥2.000 MB/s) mais em conta: <b>{html.escape(rapido['rotulo'])}</b> — "
                      f"R$ {brl_curto(rapido['por_tb'])}/TB ({oferta(rapido)})")

    if barato is not melhor and barato["preco"] < melhor["preco"]:
        economia = melhor["preco"] - barato["preco"]
        frase = (f"leve o <b>{html.escape(melhor['rotulo'])}</b>: é o menor preço por TB. "
                 f"O {html.escape(barato['rotulo'])} só compensa se o limite for o orçamento "
                 f"(economiza {brl(economia)}, mas entrega menos espaço por real).")
    else:
        frase = (f"o <b>{html.escape(melhor['rotulo'])}</b> é ao mesmo tempo o mais barato e o de "
                 f"menor preço por TB — melhor escolha agora.")
    linhas.append(f"💬 <b>Recomendação:</b> {frase}")
    return linhas, {"melhor": melhor["nome"], "barato": barato["nome"],
                    "rapido": rapido["nome"] if rapido else None}


def montar_mensagem(titulo, agora, resultados, alertas, falhas, outras_lojas=2, estilo="medalhas"):
    blocos = [f"📊 <b>{html.escape(titulo)}</b> · {agora[8:10]}/{agora[5:7]} {agora[11:16]}"]

    if alertas:
        linhas = ["🔔 <b>ALERTAS</b>"]
        for a in alertas:
            linhas.append(f"{a['icones']} <b>{html.escape(a['produto'])}</b> · {link(a['loja'], a['url'])} "
                          f"<b>{brl(a['preco'])}</b> — {html.escape(a['detalhe'])}")
        blocos.append("\n".join(linhas))

    com_preco, sem_estoque = ranking_modelos(resultados)
    capacidades = {r.get("cap") for r in resultados if r.get("cap")}
    capacidades = capacidades if len(capacidades) > 1 else set()
    grupos = secoes(com_preco, capacidades)
    for cap, modelos in grupos:
        linhas = []
        if len(grupos) > 1:
            linhas.append(f"💾 <b>{rotulo_capacidade(cap)}:</b>\n")
        sem_est_cap = [m for m in sem_estoque if (m.get("cap") or 0) == cap] if len(grupos) > 1 else []
        if not modelos:
            sem_link = [r for r in resultados if (r.get("cap") or 0) == cap and not r["ofertas"]]
            linhas.append("<i>Sem preço nesta consulta"
                          + (" — busca dos links em andamento." if sem_link else ".") + "</i>")
            if sem_est_cap:
                linhas.append("⛔ <i>Sem estoque: " + ", ".join(html.escape(m["rotulo"]) for m in sem_est_cap) + "</i>")
            blocos.append("\n".join(linhas))
            continue
        menor = modelos[0]["ofertas_ok"][0]["preco"]
        for i, m in enumerate(modelos):
            ofertas = m["ofertas_ok"]
            melhor = ofertas[0]
            marca = marcador(i, estilo)
            seta = ""
            if melhor["ultimo"] and abs(melhor["preco"] - melhor["ultimo"]) >= 0.01:
                dif = melhor["preco"] - melhor["ultimo"]
                seta = f" {'⬇️' if dif < 0 else '⬆️'}{brl_curto(abs(dif))}"
            extra = f" <i>(+{brl_curto(melhor['preco'] - menor)})</i>" if i > 0 else ""
            linhas.append(f"{marca} <b>{html.escape(m['rotulo'])}</b> — <b>{brl(melhor['preco'])}</b> · "
                          f"{link(melhor['loja'], melhor['url'])}{seta}{extra}")
            resto = ofertas[1:]
            if resto and outras_lojas:
                itens = [f"{link(o['loja'], o['url'])} {brl_curto(o['preco'])}" for o in resto[:outras_lojas]]
                if len(resto) > outras_lojas:
                    itens.append(f"+{len(resto) - outras_lojas}")
                linhas.append("      ↳ " + " · ".join(itens))
        if sem_est_cap:
            linhas.append("⛔ <i>Sem estoque: " + ", ".join(html.escape(m["rotulo"]) for m in sem_est_cap) + "</i>")
        blocos.append("\n".join(linhas))
    if sem_estoque and len(grupos) <= 1:
        txt = "⛔ <i>Sem estoque: " + ", ".join(html.escape(m["rotulo"]) for m in sem_estoque) + "</i>"
        if grupos:
            blocos[-1] += "\n" + txt
        else:
            blocos.append(txt)

    cb, _ = custo_beneficio(com_preco, capacidades)
    if cb:
        blocos.append("\n".join(cb))

    if falhas:
        cont = Counter(falhas)
        txt = " · ".join(f"{html.escape(l)}{' ×' + str(n) if n > 1 else ''}" for l, n in cont.most_common())
        blocos.append(f"<i>⚠️ Sem leitura: {txt}</i>")
    return blocos


# ============================================================ botões
LIMITE_TEXTO_BOTAO = 60     # textos maiores ficam cortados na tela do celular
MAX_BOTOES = 90             # o Telegram aceita até 100 botões por mensagem


def texto_botao(prefixo, produto, preco, loja):
    texto = f"{prefixo} {produto} · R$ {brl_curto(preco)} · {loja}"
    if len(texto) > LIMITE_TEXTO_BOTAO:
        texto = f"{prefixo} {produto} · R$ {brl_curto(preco)}"
    return texto[:LIMITE_TEXTO_BOTAO]


def montar_botoes(resultados, alertas, alternativas=0, estilo="medalhas"):
    """1 botão por modelo (melhor oferta, na ordem do ranking) + botões das ofertas em alerta no topo.
    `alternativas` > 0 acrescenta, na mesma linha, as próximas lojas mais baratas."""
    linhas, usados = [], set()
    for a in alertas:
        if a["url"] in usados:
            continue
        usados.add(a["url"])
        linhas.append([{"text": texto_botao(a["icones"], a["produto"], a["preco"], a["loja"]), "url": a["url"]}])

    com_preco, _ = ranking_modelos(resultados)
    for cap, modelos in secoes(com_preco):
        for i, m in enumerate(modelos):
            ofertas = m["ofertas_ok"]
            marca = marcador(i, estilo)
            melhor = ofertas[0]
            linha = [{"text": texto_botao(marca, m["rotulo"], melhor["preco"], melhor["loja"]), "url": melhor["url"]}]
            for o in ofertas[1:1 + alternativas]:
                linha.append({"text": f"{o['loja']} {brl_curto(o['preco'])}"[:LIMITE_TEXTO_BOTAO], "url": o["url"]})
            linhas.append(linha)

    total, saida = 0, []
    for linha in linhas:
        if total + len(linha) > MAX_BOTOES:
            break
        saida.append(linha)
        total += len(linha)
    return saida


# ============================================================ execução
def executar(cfg, historico, coletor, agora, estado=None, extras=None):
    """Consulta todos os links. Devolve (novas_linhas, blocos, tem_alerta, mudou)."""
    estado = estado or {}
    queda_pct = cfg.get("queda_minima_pct", 3)
    margem_minimo = cfg.get("margem_menor_preco_pct", 1)
    ult_url = ultimo_por_url(historico)
    novas, alertas, resultados, falhas = [], [], [], []
    mudou = False

    for prod in cfg["produtos"]:
        nome, alvo = prod["nome"], prod.get("preco_alvo")
        alvo_min = prod.get("preco_alvo_min")
        faixa = prod.get("faixa_valida", [1, 1_000_000])
        precos_hist = [float(h["preco"]) for h in historico
                       if h.get("produto") == nome and h.get("disponivel") == "1"]
        minimo_hist = min(precos_hist) if precos_hist else None
        ofertas, alertas_modelo = [], []

        for oferta in ofertas_do_produto(prod, estado):
            url, loja = oferta["url"], oferta["loja"]
            ja_em_cache = url in coletor.cache
            preco, disp, metodo = coletor.coletar(url, faixa, oferta.get("seletor"),
                                                  produto=None if oferta["fixa"] else prod)
            anterior = ult_url.get(url)
            ultimo_disp = anterior[0] if anterior and anterior[1] else None
            ofertas.append({"loja": loja, "url": url, "preco": preco, "disp": disp, "ultimo": ultimo_disp})
            if not ja_em_cache:
                pausa(1.5)

            if preco is None and disp is None:
                print(f"✗ {nome} | {loja}: {metodo}")
                if "pulado" not in metodo or loja not in falhas:
                    falhas.append(loja)
                continue
            if preco is None:
                print(f"⛔ {nome} | {loja}: esgotado")
                if anterior and anterior[1]:
                    mudou = True
                continue

            print(f"✓ {nome} | {loja}: {brl(preco)} ({'disponível' if disp else 'esgotado'}, via {metodo})")
            novas.append({"data_hora": agora, "produto": nome, "loja": loja,
                          "preco": f"{preco:.2f}", "disponivel": "1" if disp else "0", "url": url})
            if anterior is None or abs(anterior[0] - preco) >= 0.01 or anterior[1] != disp:
                mudou = True
            if not disp:
                continue

            icones, detalhes = "", []
            if alvo and preco <= alvo and (ultimo_disp is None or ultimo_disp > alvo):
                icones += "🎯"
                if alvo_min and preco < alvo_min:
                    detalhes.append(f"abaixo da faixa-alvo {brl_curto(alvo_min)}–{brl_curto(alvo)}")
                elif alvo_min:
                    detalhes.append(f"na faixa-alvo {brl_curto(alvo_min)}–{brl_curto(alvo)}")
                else:
                    detalhes.append(f"alvo {brl(alvo)}")
            if ultimo_disp and preco <= ultimo_disp * (1 - queda_pct / 100):
                icones += "📉"
                detalhes.append(f"-{(1 - preco / ultimo_disp) * 100:.0f}% (era {brl_curto(ultimo_disp)})")
            if minimo_hist and preco <= minimo_hist * (1 - margem_minimo / 100):
                icones += "🏆"
                detalhes.append("menor preço já visto")
            if icones:
                alertas_modelo.append({"produto": rotulo_produto(prod), "loja": loja, "url": url, "preco": preco,
                                       "icones": icones, "detalhes": detalhes})

        if alertas_modelo:   # 1 alerta por modelo: a oferta mais barata, reunindo os motivos de todas
            principal = min(alertas_modelo, key=lambda a: a["preco"])
            icones = "".join(dict.fromkeys(c for a in alertas_modelo for c in a["icones"]))
            detalhes = list(dict.fromkeys(principal["detalhes"] + [d for a in alertas_modelo for d in a["detalhes"]
                                                                   if not d.startswith(("na faixa", "abaixo da faixa",
                                                                                        "alvo", "-"))]))
            alertas.append({**{k: v for k, v in principal.items() if k != "detalhes"},
                            "icones": icones, "detalhe": " · ".join(detalhes)})

        resultados.append({"nome": nome, "ofertas": ofertas, "rotulo": rotulo_produto(prod),
                           "cap": prod.get("capacidade_gb"), "par": prod.get("par_1tb"),
                           "par_aproximado": prod.get("par_aproximado", False), "vel": prod.get("leitura_mbs")})

    estilo = cfg.get("estilo_ranking", "medalhas")
    blocos = montar_mensagem(cfg.get("titulo", "Monitor de preços"), agora, resultados, alertas, falhas,
                             cfg.get("outras_lojas_por_modelo", 2), estilo)
    if extras is not None:
        b = cfg.get("botoes", {"ativo": True})
        extras["botoes"] = (montar_botoes(resultados, alertas, b.get("alternativas_por_modelo", 0), estilo)
                            if b.get("ativo", True) else [])
    return novas, blocos, bool(alertas), mudou


def deve_enviar(modo, tem_alerta, mudou, forcar):
    if forcar or tem_alerta:
        return True
    if modo == "sempre":
        return True
    if modo == "se_mudar":
        return mudou
    return False  # "so_alertas"


def main(caminho_config=None, caminho_historico=None, coletor=None, caminho_estado=None):
    cfg = json.loads(Path(caminho_config or CONFIG).read_text(encoding="utf-8"))
    historico = ler_historico(caminho_historico)
    if caminho_estado is None and caminho_historico is not None:
        caminho_estado = Path(caminho_historico).with_name("estado.json")
    estado = ler_estado(caminho_estado)
    historico, removidas, trocou = aplicar_versao_regras(cfg, estado, historico)
    if removidas:
        reescrever_historico(historico, caminho_historico)
    if trocou:
        print(f"♻️ Regras de busca atualizadas: links descobertos zerados, {removidas} preço(s) antigo(s) removido(s).")
    agora = agora_br()
    coletor = coletor or Coletor(cfg.get("usar_leitor_reserva", False),
                                 cfg.get("usar_navegador_real", True),
                                 cfg.get("dominios_navegador", ["*"]))
    try:
        estado = descobrir(cfg, estado, coletor, agora)
        extras = {}
        novas, blocos, tem_alerta, mudou = executar(cfg, historico, coletor, agora, estado, extras)
    finally:
        coletor.fechar()

    estado["ultima_execucao"] = agora
    gravar_estado(estado, caminho_estado)
    if novas:
        gravar_historico(novas, caminho_historico)

    forcar = os.getenv("ENVIAR_RESUMO", "").lower() == "true"
    modo = cfg.get("enviar_resumo", "se_mudar")
    enviado = False
    if deve_enviar(modo, tem_alerta, mudou, forcar):
        mensagens = dividir_mensagem(blocos)
        for i, msg in enumerate(mensagens):
            botoes = extras.get("botoes") if i == len(mensagens) - 1 else None
            enviado = telegram(msg, silencioso=not tem_alerta, botoes=botoes) or enviado
    else:
        print(f"Sem mudanças — mensagem não enviada (modo '{modo}').")

    print(f"\nResumo: {len(novas)} preços lidos · alertas: {'sim' if tem_alerta else 'não'} · "
          f"mudou: {'sim' if mudou else 'não'}")
    return {"novas": novas, "blocos": blocos, "alerta": tem_alerta, "mudou": mudou,
            "enviado": enviado, "estado": estado, "botoes": extras.get("botoes", [])}


if __name__ == "__main__":
    main()
