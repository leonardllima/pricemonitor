#!/usr/bin/env python3
"""
Monitor de preços (GitHub Actions + Telegram)

Fluxo de cada execução:
  1. Lê config.json (produtos, lojas, preço-alvo).
  2. Para cada oferta, baixa a página imitando um navegador (curl_cffi).
     Se a loja bloquear, abre a página num Chrome real (Playwright), que resolve
     desafios de JavaScript como o "Just a moment..." do Cloudflare.
     Opcionalmente tenta ainda o Jina Reader (leitor de reserva).
     Se uma loja bloquear 2 links seguidos, ela é pulada no resto da execução.
  3. Extrai o preço (seletor próprio > JSON-LD > meta tags > seletores da loja).
  4. Grava em historico.csv e calcula alertas (alvo, queda %, menor preço já visto).
  5. Envia UMA mensagem compacta no Telegram (com som só se houver alerta).
"""
import csv
import html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:  # imita a conexão TLS de um navegador real
    from curl_cffi import requests as navegador
except ImportError:  # pragma: no cover
    navegador = None

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
HISTORICO = BASE / "historico.csv"
CAMPOS = ["data_hora", "produto", "loja", "preco", "disponivel", "url"]
FUSO_BR = timezone(timedelta(hours=-3))
LIMITE_TELEGRAM = 3900          # caracteres visíveis por mensagem (limite real: 4096)
FALHAS_PARA_DESISTIR = 2        # links seguidos com falha antes de pular a loja
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
    "shopee.": ['[class*="pqTWkA"]', '[class*="G27FPf"]'],
    "aliexpress.": ['[class*="price--currentPriceText"]', ".product-price-value"],
}

PAGINA_BLOQUEIO = ("robot check", "just a moment", "attention required", "access denied",
                   "captcha", "verifica", "are you a human", "mercado libre")


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
    """1149.9 -> '1.150' (sem centavos, para as linhas secundárias)."""
    return f"{round(v):,}".replace(",", ".")


def dominio(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def link(texto, url):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(texto)}</a>'


def texto_visivel(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def _nos(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nos(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _nos(item)


# ============================================================ extração
def precos_jsonld(soup):
    """Retorna ([(preco, disponivel)], esgotado_sem_preco)."""
    achados, esgotado_sem_preco = [], False
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
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
    """Retorna (preco, disponivel, metodo).
    - preço encontrado: (float, True/False, metodo)
    - esgotado sem preço: (None, False, 'esgotado')
    - falha: (None, None, motivo)"""
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
    if any(m in titulo.lower() for m in PAGINA_BLOQUEIO):
        return None, None, f"bloqueado (página '{titulo}')"
    return None, None, f"preço não encontrado (título: '{titulo}')"


# ============================================================ coleta
class Coletor:
    """Baixa páginas em camadas (direto -> Chrome real -> leitor) e desiste de lojas
    que bloqueiam repetidamente na mesma execução."""

    def __init__(self, usar_leitor=True, usar_navegador=False, dominios_navegador=None):
        self.usar_leitor = usar_leitor
        self.usar_navegador = usar_navegador
        self.dominios_navegador = dominios_navegador or ["*"]
        self.sessao = None
        self._pw = self._browser = self._contexto = None
        self.falhas_direto = Counter()   # falhas seguidas no acesso direto, por domínio
        self.falhas_total = Counter()    # falhas seguidas em todas as camadas, por domínio

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
            except Exception as e:  # erros de rede de qualquer biblioteca
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
                # dá até ~24 s para o desafio se resolver e o preço aparecer
                for _ in range(12):
                    titulo = (pagina.title() or "").lower()
                    tem_preco = pagina.locator('script[type="application/ld+json"], '
                                               'meta[itemprop="price"], '
                                               'meta[property="product:price:amount"]').count() > 0
                    if tem_preco and not any(m in titulo for m in PAGINA_BLOQUEIO):
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

    # ---- lógica
    def _usa_navegador(self, dom):
        return self.usar_navegador and any(d == "*" or d in dom for d in self.dominios_navegador)

    def coletar(self, url, faixa, seletor=None):
        dom = dominio(url)
        if self.falhas_total[dom] >= FALHAS_PARA_DESISTIR:
            return None, None, "pulado (loja bloqueou nesta execução)"

        camadas = []
        if self.falhas_direto[dom] < FALHAS_PARA_DESISTIR:
            camadas.append(("direto", self.baixar))
        if self._usa_navegador(dom):
            camadas.append(("navegador", self.baixar_via_navegador))
        if self.usar_leitor:
            camadas.append(("leitor", self.baixar_via_leitor))

        motivos = []
        for nome, funcao in camadas:
            pagina, erro = funcao(url)
            if pagina:
                preco, disp, m = extrair_preco(pagina, url, faixa, seletor)
                if preco is not None or disp is False:
                    if nome == "direto":
                        self.falhas_direto[dom] = 0
                    self.falhas_total[dom] = 0
                    return preco, disp, m if nome == "direto" else f"{m} pelo {nome}"
                motivos.append(f"{nome}: {m}")
            else:
                motivos.append(f"{nome}: {erro}")
                if erro == "HTTP 404":   # link quebrado: não adianta insistir nas outras camadas
                    break
            if nome == "direto":
                self.falhas_direto[dom] += 1

        self.falhas_total[dom] += 1
        return None, None, " | ".join(motivos)


# ============================================================ histórico
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


def ultimo_por_url(historico):
    """url -> (preco, disponivel) da leitura mais recente."""
    ult = {}
    for h in historico:
        try:
            ult[h["url"]] = (float(h["preco"]), h["disponivel"] == "1")
        except (ValueError, KeyError):
            continue
    return ult


# ============================================================ telegram
def telegram(texto, silencioso=False):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram não configurado — mensagem abaixo]\n" + texto_visivel(texto))
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
            "chat_id": chat, "text": texto, "parse_mode": "HTML",
            "disable_web_page_preview": "true", "disable_notification": "true" if silencioso else "false"})
        if r.status_code != 200:
            print(f"Falha no Telegram: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Falha no Telegram: {e}")
        return False


def dividir_mensagem(blocos, limite=LIMITE_TELEGRAM):
    """Agrupa blocos em mensagens sem passar do limite de texto visível."""
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


def montar_mensagem(titulo, agora, resultados, alertas, falhas, outras_lojas=3):
    """Mensagem compacta: alertas (se houver) + 1 linha por modelo + rodapé de falhas."""
    blocos = [f"📊 <b>{html.escape(titulo)}</b> · {agora[8:10]}/{agora[5:7]} {agora[11:16]}"]

    if alertas:
        linhas = ["🔔 <b>ALERTAS</b>"]
        for a in alertas:
            linhas.append(f"{a['icones']} <b>{html.escape(a['produto'])}</b> · {link(a['loja'], a['url'])} "
                          f"<b>{brl(a['preco'])}</b> — {html.escape(a['detalhe'])}")
        blocos.append("\n".join(linhas))

    com_preco, sem_estoque = [], []
    for r in resultados:
        disp = sorted([o for o in r["ofertas"] if o["preco"] is not None and o["disp"]],
                      key=lambda o: o["preco"])
        (com_preco if disp else sem_estoque).append((r["nome"], disp))
    com_preco.sort(key=lambda x: x[1][0]["preco"])

    linhas = []
    menor_geral = com_preco[0][1][0]["preco"] if com_preco else None
    for i, (nome, ofertas) in enumerate(com_preco):
        melhor = ofertas[0]
        marca = MEDALHAS[i] if i < len(MEDALHAS) else f"{i + 1}."
        seta = ""
        if melhor["ultimo"] and abs(melhor["preco"] - melhor["ultimo"]) >= 0.01:
            dif = melhor["preco"] - melhor["ultimo"]
            seta = f" {'⬇️' if dif < 0 else '⬆️'}{brl_curto(abs(dif))}"
        extra = f" <i>(+{brl_curto(melhor['preco'] - menor_geral)})</i>" if i > 0 else ""
        linhas.append(f"{marca} <b>{html.escape(nome)}</b> — <b>{brl(melhor['preco'])}</b> · "
                      f"{link(melhor['loja'], melhor['url'])}{seta}{extra}")
        resto = ofertas[1:]
        if resto:
            itens = [f"{link(o['loja'], o['url'])} {brl_curto(o['preco'])}" for o in resto[:outras_lojas]]
            if len(resto) > outras_lojas:
                itens.append(f"+{len(resto) - outras_lojas}")
            linhas.append("      ↳ " + " · ".join(itens))
    for nome, _ in sem_estoque:
        linhas.append(f"⛔ <b>{html.escape(nome)}</b> — sem estoque nas lojas lidas")
    if linhas:
        blocos.append("\n".join(linhas))

    if falhas:
        cont = Counter(falhas)
        txt = " · ".join(f"{html.escape(l)}{' ×' + str(n) if n > 1 else ''}" for l, n in cont.most_common())
        blocos.append(f"<i>⚠️ Sem leitura: {txt}</i>")
    return blocos


# ============================================================ execução
def executar(cfg, historico, coletor, agora):
    """Coleta tudo e devolve (novas_linhas, blocos_mensagem, tem_alerta, mudou)."""
    queda_pct = cfg.get("queda_minima_pct", 3)
    margem_minimo = cfg.get("margem_menor_preco_pct", 1)   # evita alerta por centavos
    ult_url = ultimo_por_url(historico)
    novas, alertas, resultados, falhas = [], [], [], []
    mudou = False

    for prod in cfg["produtos"]:
        nome, alvo = prod["nome"], prod.get("preco_alvo")
        faixa = prod.get("faixa_valida", [1, 1_000_000])
        precos_hist = [float(h["preco"]) for h in historico
                       if h.get("produto") == nome and h.get("disponivel") == "1"]
        minimo_hist = min(precos_hist) if precos_hist else None
        ofertas = []

        for oferta in prod["ofertas"]:
            url = (oferta.get("url") or "").strip()
            if not url.startswith("http") or oferta.get("ativo") is False:
                continue
            loja = oferta["loja"]
            preco, disp, metodo = coletor.coletar(url, faixa, oferta.get("seletor"))
            anterior = ult_url.get(url)
            ultimo_disp = anterior[0] if anterior and anterior[1] else None
            ofertas.append({"loja": loja, "url": url, "preco": preco, "disp": disp, "ultimo": ultimo_disp})
            pausa(1.5)

            if preco is None and disp is None:
                print(f"✗ {nome} | {loja}: {metodo}")
                falhas.append(loja)
                continue
            if preco is None:  # esgotado sem preço publicado
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
                detalhes.append(f"alvo {brl(alvo)}")
            if ultimo_disp and preco <= ultimo_disp * (1 - queda_pct / 100):
                icones += "📉"
                detalhes.append(f"-{(1 - preco / ultimo_disp) * 100:.0f}% (era {brl_curto(ultimo_disp)})")
            if minimo_hist and preco <= minimo_hist * (1 - margem_minimo / 100):
                icones += "🏆"
                detalhes.append("menor preço já visto")
            if icones:
                alertas.append({"produto": nome, "loja": loja, "url": url, "preco": preco,
                                "icones": icones, "detalhe": " · ".join(detalhes)})

        resultados.append({"nome": nome, "ofertas": ofertas})

    blocos = montar_mensagem(cfg.get("titulo", "Monitor de preços"), agora, resultados, alertas, falhas)
    return novas, blocos, bool(alertas), mudou


def deve_enviar(modo, tem_alerta, mudou, forcar):
    if forcar or tem_alerta:
        return True
    if modo == "sempre":
        return True
    if modo == "se_mudar":
        return mudou
    return False  # "so_alertas"


def main(caminho_config=None, caminho_historico=None, coletor=None):
    cfg = json.loads(Path(caminho_config or CONFIG).read_text(encoding="utf-8"))
    historico = ler_historico(caminho_historico)
    agora = datetime.now(FUSO_BR).strftime("%Y-%m-%d %H:%M")
    coletor = coletor or Coletor(cfg.get("usar_leitor_reserva", False),
                                 cfg.get("usar_navegador_real", True),
                                 cfg.get("dominios_navegador", ["*"]))
    try:
        novas, blocos, tem_alerta, mudou = executar(cfg, historico, coletor, agora)
    finally:
        coletor.fechar()
    if novas:
        gravar_historico(novas, caminho_historico)

    forcar = os.getenv("ENVIAR_RESUMO", "").lower() == "true"
    modo = cfg.get("enviar_resumo", "se_mudar")
    enviado = False
    if deve_enviar(modo, tem_alerta, mudou, forcar):
        for msg in dividir_mensagem(blocos):
            enviado = telegram(msg, silencioso=not tem_alerta) or enviado
    else:
        print(f"Sem mudanças — mensagem não enviada (modo '{modo}').")

    print(f"\nResumo: {len(novas)} preços lidos · alertas: {'sim' if tem_alerta else 'não'} · "
          f"mudou: {'sim' if mudou else 'não'}")
    return {"novas": novas, "blocos": blocos, "alerta": tem_alerta, "mudou": mudou, "enviado": enviado}


if __name__ == "__main__":
    main()
