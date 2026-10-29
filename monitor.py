#!/usr/bin/env python3
"""
Monitor de preços
- Lê os produtos/lojas do config.json
- Extrai o preço de cada página (seletor próprio -> JSON-LD -> meta tags -> seletores por loja)
- Grava tudo em historico.csv
- Envia alerta no Telegram quando: atinge o preço-alvo, cai X% ou bate o menor preço histórico
"""
import csv
import html
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
HISTORICO = BASE / "historico.csv"
CAMPOS = ["data_hora", "produto", "loja", "preco", "disponivel", "url"]
FUSO_BR = timezone(timedelta(hours=-3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Seletores de reserva por domínio (usados só se JSON-LD e meta tags falharem)
SELETORES_LOJA = {
    "amazon.": [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#price_inside_buybox",
        ".a-price .a-offscreen",
    ],
    "kabum.": ["h4.finalPrice", ".finalPrice"],
    "mercadolivre.": ['meta[itemprop="price"]'],
    "pichau.": ['[data-cy="product-price"]'],
    "terabyteshop.": ["#valVista", ".val-prod"],
}


# ----------------------------------------------------------------- utilidades
def parse_preco(valor):
    """Converte '1.234,56', 'R$ 399,90', '399.90' ou 399.9 em float."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                    # 1,234.56
            s = s.replace(",", "")
    elif "," in s:                               # 399,90
        s = s.replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):  # 1.299
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _nos(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nos(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _nos(item)


# ----------------------------------------------------------------- extração
def precos_jsonld(soup):
    """Retorna [(preco, disponivel)] dos blocos schema.org Offer."""
    achados = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for no in _nos(data):
            tipo = no.get("@type")
            tipos = tipo if isinstance(tipo, list) else [tipo]
            if not any(t in ("Offer", "AggregateOffer") for t in tipos):
                continue
            disp = "outofstock" not in str(no.get("availability", "")).lower()
            for chave in ("price", "lowPrice"):
                p = parse_preco(no.get(chave))
                if p:
                    achados.append((p, disp))
                    break
    return achados


def precos_seletores(soup, seletores):
    achados = []
    for sel in seletores:
        for el in soup.select(sel):
            p = parse_preco(el.get("content") or el.get_text(" ", strip=True))
            if p:
                achados.append(p)
        if achados:
            break
    return achados


META = [
    'meta[property="product:price:amount"]',
    'meta[property="og:price:amount"]',
    'meta[itemprop="price"]',
    '[itemprop="price"]',
]


def extrair_preco(html_txt, url, faixa, seletor_custom=None):
    """Retorna (preco, disponivel, metodo) ou (None, None, motivo)."""
    soup = BeautifulSoup(html_txt, "html.parser")
    lo, hi = faixa
    ok = lambda p: p is not None and lo <= p <= hi

    if seletor_custom:
        c = [p for p in precos_seletores(soup, [seletor_custom]) if ok(p)]
        if c:
            return c[0], True, "seletor"

    c = [(p, d) for p, d in precos_jsonld(soup) if ok(p)]
    if c:
        return c[0][0], c[0][1], "json-ld"

    c = [p for p in precos_seletores(soup, META) if ok(p)]
    if c:
        return c[0], True, "meta"

    for dominio, sels in SELETORES_LOJA.items():
        if dominio in url:
            c = [p for p in precos_seletores(soup, sels) if ok(p)]
            if c:
                return c[0], True, "loja"

    titulo = (soup.title.get_text(strip=True) if soup.title else "")[:60]
    return None, None, f"preço não encontrado (título: '{titulo}' — possível bloqueio)"


def baixar(url, tentativas=2):
    erro = ""
    for i in range(tentativas):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.text, None
            erro = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            erro = str(e)[:120]
        time.sleep(3 + 4 * i)
    return None, erro


# ----------------------------------------------------------------- histórico
def ler_historico():
    if not HISTORICO.exists():
        return []
    with HISTORICO.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def gravar(linhas):
    novo = not HISTORICO.exists()
    with HISTORICO.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo:
            w.writeheader()
        w.writerows(linhas)


# ----------------------------------------------------------------- telegram
def telegram(texto):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram não configurado — mensagem abaixo]\n" + texto)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": texto, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=20,
        ).raise_for_status()
    except requests.RequestException as e:
        print(f"Falha ao enviar Telegram: {e}")


# ----------------------------------------------------------------- principal
def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    queda_pct = cfg.get("queda_minima_pct", 3)
    historico = ler_historico()
    agora = datetime.now(FUSO_BR).strftime("%Y-%m-%d %H:%M")
    novas, alertas, resumo = [], [], []

    for prod in cfg["produtos"]:
        nome = prod["nome"]
        alvo = prod.get("preco_alvo")
        faixa = prod.get("faixa_valida", [1, 1_000_000])
        precos_hist = [float(h["preco"]) for h in historico
                       if h["produto"] == nome and h["disponivel"] == "1"]
        minimo_hist = min(precos_hist) if precos_hist else None
        melhor = None

        for oferta in prod["ofertas"]:
            loja, url = oferta["loja"], oferta["url"]
            pagina, erro = baixar(url)
            if pagina:
                preco, disp, metodo = extrair_preco(pagina, url, faixa, oferta.get("seletor"))
            else:
                preco, disp, metodo = None, None, erro

            if preco is None:
                print(f"✗ {nome} | {loja}: {metodo}")
                continue
            print(f"✓ {nome} | {loja}: {brl(preco)} ({'disponível' if disp else 'esgotado'}, via {metodo})")
            novas.append({"data_hora": agora, "produto": nome, "loja": loja,
                          "preco": f"{preco:.2f}", "disponivel": "1" if disp else "0", "url": url})
            if not disp:
                continue

            if melhor is None or preco < melhor[0]:
                melhor = (preco, loja, url)

            ant = [float(h["preco"]) for h in historico
                   if h["url"] == url and h["disponivel"] == "1"]
            ultimo = ant[-1] if ant else None
            motivos = []
            if alvo and preco <= alvo and (ultimo is None or ultimo > alvo):
                motivos.append(f"🎯 atingiu o alvo de {brl(alvo)}")
            if ultimo and preco <= ultimo * (1 - queda_pct / 100):
                motivos.append(f"📉 caiu {(1 - preco / ultimo) * 100:.1f}% (antes {brl(ultimo)})")
            if minimo_hist and preco < minimo_hist:
                motivos.append(f"🏆 menor preço já registrado (anterior {brl(minimo_hist)})")
            if motivos:
                alertas.append(
                    f"<b>{html.escape(nome)}</b> — {html.escape(loja)}: <b>{brl(preco)}</b>\n"
                    + "\n".join(motivos) + f'\n<a href="{html.escape(url)}">Abrir oferta</a>'
                )

        if melhor:
            resumo.append(f"• {html.escape(nome)}: {brl(melhor[0])} na {html.escape(melhor[1])}")

    if novas:
        gravar(novas)
    if alertas:
        telegram("🔔 <b>Alerta de preço</b>\n\n" + "\n\n".join(alertas))
    if os.getenv("ENVIAR_RESUMO", "").lower() == "true":
        telegram("📊 <b>Resumo atual</b>\n" + ("\n".join(resumo) or "Nenhum preço coletado."))
    if not novas:
        print("⚠ Nenhum preço coletado nesta execução.")


if __name__ == "__main__":
    main()
