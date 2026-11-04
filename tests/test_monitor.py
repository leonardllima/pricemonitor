"""Testes automáticos do monitor (rodam offline, sem acessar lojas nem Telegram)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

monitor.ESPERA = 0  # sem pausas nos testes
FAIXA = [300, 3000]


def jsonld(preco, disp="https://schema.org/InStock", grafo=False):
    oferta = {"@type": "Offer", "price": preco, "priceCurrency": "BRL", "availability": disp}
    prod = {"@type": "Product", "name": "SSD", "offers": oferta}
    data = {"@graph": [prod]} if grafo else prod
    return f'<html><head><title>SSD</title><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


def meta(preco):
    return f'<html><head><title>SSD</title><meta itemprop="price" content="{preco}"></head></html>'


class ColetorFalso(monitor.Coletor):
    """Coletor que responde a partir de um dicionário url -> preço (ou None = bloqueio)."""

    def __init__(self, respostas, leitor=None, usar_leitor=True, navegador=None, dominios_navegador=None):
        super().__init__(usar_leitor, usar_navegador=navegador is not None,
                         dominios_navegador=dominios_navegador)
        self.respostas, self.leitor, self.nav = respostas, leitor or {}, navegador or {}
        self.chamadas_direto, self.chamadas_leitor, self.chamadas_nav = [], [], []

    def _pagina(self, valor):
        if valor is None:
            return None, "HTTP 403"
        if valor == 404:
            return None, "HTTP 404"
        if valor == "desafio":
            return "<html><head><title>Just a moment...</title></head></html>", None
        if valor == "esgotado":
            return jsonld(0, "https://schema.org/OutOfStock"), None
        if isinstance(valor, tuple):
            return jsonld(valor[0], "https://schema.org/OutOfStock"), None
        return meta(valor), None

    def baixar(self, url, tentativas=2):
        self.chamadas_direto.append(url)
        return self._pagina(self.respostas.get(url))

    def baixar_via_leitor(self, url):
        self.chamadas_leitor.append(url)
        return self._pagina(self.leitor.get(url))

    def baixar_via_navegador(self, url):
        self.chamadas_nav.append(url)
        return self._pagina(self.nav.get(url))


def cfg_teste(alvo=900, ofertas=None):
    return {"titulo": "Teste", "queda_minima_pct": 3, "enviar_resumo": "se_mudar", "produtos": [
        {"nome": "Modelo A", "preco_alvo": alvo, "faixa_valida": FAIXA,
         "ofertas": ofertas or [{"loja": "Loja1", "url": "https://loja1.com/a"},
                                {"loja": "Loja2", "url": "https://loja2.com/a"}]},
        {"nome": "Modelo B", "preco_alvo": alvo, "faixa_valida": FAIXA,
         "ofertas": [{"loja": "Loja1", "url": "https://loja1.com/b"},
                     {"loja": "Shopee", "url": ""}]}]}


# ------------------------------------------------------------------ preço
class TestParsePreco(unittest.TestCase):
    def test_formatos(self):
        casos = {"R$ 1.234,56": 1234.56, "399,90": 399.9, "399.90": 399.9, "1.299": 1299.0,
                 "R$ 2.019,00": 2019.0, "1,234.56": 1234.56, 429.9: 429.9, 1000: 1000.0}
        for entrada, esperado in casos.items():
            self.assertEqual(monitor.parse_preco(entrada), esperado, entrada)

    def test_invalidos(self):
        for v in (None, "", "abc", 0, "0", "0,00", True):
            self.assertIsNone(monitor.parse_preco(v), v)

    def test_formatacao(self):
        self.assertEqual(monitor.brl(1234.5), "R$ 1.234,50")
        self.assertEqual(monitor.brl_curto(1149.6), "1.150")


# ------------------------------------------------------------------ extração
class TestExtracao(unittest.TestCase):
    def test_jsonld_disponivel(self):
        self.assertEqual(monitor.extrair_preco(jsonld("459.90"), "https://x.com", FAIXA), (459.9, True, "json-ld"))

    def test_jsonld_grafo_americanas(self):
        self.assertEqual(monitor.extrair_preco(jsonld("930.79", grafo=True), "https://x.com", FAIXA)[0], 930.79)

    def test_esgotado_com_preco(self):
        p, d, _ = monitor.extrair_preco(jsonld(449, "OutOfStock"), "https://x.com", FAIXA)
        self.assertEqual((p, d), (449.0, False))

    def test_esgotado_sem_preco(self):
        self.assertEqual(monitor.extrair_preco(jsonld(0, "https://schema.org/OutOfStock"), "https://x.com", FAIXA),
                         (None, False, "esgotado"))

    def test_meta(self):
        self.assertEqual(monitor.extrair_preco(meta("419.00"), "https://x.com", FAIXA), (419.0, True, "meta"))

    def test_amazon_ignora_parcela(self):
        pagina = ('<div id="corePriceDisplay_desktop_feature_div"><span class="a-price">'
                  '<span class="a-offscreen">R$ 1.049,90</span></span></div>'
                  '<span class="a-price"><span class="a-offscreen">R$ 87,49</span></span>')
        self.assertEqual(monitor.extrair_preco(pagina, "https://www.amazon.com.br/dp/X", FAIXA)[0], 1049.9)

    def test_faixa_descarta_valor_absurdo(self):
        self.assertIsNone(monitor.extrair_preco(meta("36.66"), "https://x.com", FAIXA)[0])

    def test_seletor_proprio(self):
        pagina = '<span class="preco">R$ 499,00</span>' + jsonld("999")
        self.assertEqual(monitor.extrair_preco(pagina, "https://x.com", FAIXA, ".preco")[0], 499.0)

    def test_pagina_de_bloqueio(self):
        p, d, motivo = monitor.extrair_preco("<title>Robot Check</title>", "https://x.com", FAIXA)
        self.assertIsNone(p)
        self.assertIn("bloqueado", motivo)


# ------------------------------------------------------------------ coleta
class TestColetor(unittest.TestCase):
    def test_direto_ok(self):
        c = ColetorFalso({"https://a.com/1": 500})
        self.assertEqual(c.coletar("https://a.com/1", FAIXA)[0], 500.0)
        self.assertEqual(c.chamadas_leitor, [])

    def test_reserva_pelo_leitor(self):
        c = ColetorFalso({}, leitor={"https://a.com/1": 600})
        p, d, metodo = c.coletar("https://a.com/1", FAIXA)
        self.assertEqual(p, 600.0)
        self.assertIn("pelo leitor", metodo)

    def test_leitor_desligado(self):
        c = ColetorFalso({}, leitor={"https://a.com/1": 600}, usar_leitor=False)
        self.assertIsNone(c.coletar("https://a.com/1", FAIXA)[0])
        self.assertEqual(c.chamadas_leitor, [])

    def test_desiste_da_loja_apos_duas_falhas(self):
        c = ColetorFalso({})
        c.coletar("https://bloq.com/1", FAIXA)
        c.coletar("https://bloq.com/2", FAIXA)
        p, d, motivo = c.coletar("https://bloq.com/3", FAIXA)
        self.assertIn("pulado", motivo)
        self.assertNotIn("https://bloq.com/3", c.chamadas_direto + c.chamadas_leitor)

    def test_pula_acesso_direto_quando_so_leitor_funciona(self):
        leitor = {"https://l.com/1": 500, "https://l.com/2": 510, "https://l.com/3": 520}
        c = ColetorFalso({}, leitor=leitor)
        for u in leitor:
            self.assertIsNotNone(c.coletar(u, FAIXA)[0])
        self.assertEqual(c.chamadas_direto, ["https://l.com/1", "https://l.com/2"])

    def test_sucesso_zera_contador(self):
        c = ColetorFalso({"https://a.com/2": 500})
        c.coletar("https://a.com/1", FAIXA)
        c.coletar("https://a.com/2", FAIXA)
        c.coletar("https://a.com/3", FAIXA)
        self.assertNotIn("pulado", c.coletar("https://a.com/4", FAIXA)[2])

    def test_navegador_resolve_desafio(self):
        url = "https://www.pichau.com.br/x"
        c = ColetorFalso({url: "desafio"}, usar_leitor=False, navegador={url: 899})
        p, d, metodo = c.coletar(url, FAIXA)
        self.assertEqual(p, 899.0)
        self.assertIn("pelo navegador", metodo)

    def test_navegador_antes_do_leitor(self):
        url = "https://a.com/1"
        c = ColetorFalso({}, leitor={url: 700}, navegador={url: 650})
        self.assertEqual(c.coletar(url, FAIXA)[0], 650.0)
        self.assertEqual(c.chamadas_leitor, [])

    def test_navegador_so_nos_dominios_configurados(self):
        c = ColetorFalso({}, usar_leitor=False, navegador={"https://outra.com/1": 500},
                         dominios_navegador=["pichau.com.br"])
        self.assertIsNone(c.coletar("https://outra.com/1", FAIXA)[0])
        self.assertEqual(c.chamadas_nav, [])

    def test_link_quebrado_nao_insiste(self):
        url = "https://a.com/1"
        c = ColetorFalso({url: 404}, leitor={url: 500}, navegador={url: 500})
        p, d, motivo = c.coletar(url, FAIXA)
        self.assertIsNone(p)
        self.assertIn("404", motivo)
        self.assertEqual(c.chamadas_nav + c.chamadas_leitor, [])

    def test_navegador_continua_apos_bloqueio_direto(self):
        urls = [f"https://www.terabyteshop.com.br/{i}" for i in range(4)]
        c = ColetorFalso({}, usar_leitor=False, navegador={u: 900 + i for i, u in enumerate(urls)})
        for u in urls:
            self.assertIsNotNone(c.coletar(u, FAIXA)[0])
        self.assertEqual(len(c.chamadas_direto), 2)   # para de tentar direto após 2 bloqueios
        self.assertEqual(len(c.chamadas_nav), 4)

    def test_esgotado_nao_conta_como_falha(self):
        c = ColetorFalso({"https://a.com/1": "esgotado", "https://a.com/2": "esgotado", "https://a.com/3": 500})
        c.coletar("https://a.com/1", FAIXA)
        c.coletar("https://a.com/2", FAIXA)
        self.assertEqual(c.coletar("https://a.com/3", FAIXA)[0], 500.0)


# ------------------------------------------------------------------ alertas e execução
class TestExecucao(unittest.TestCase):
    AGORA = "2026-10-30 14:00"

    def rodar(self, respostas, historico=None, cfg=None):
        return monitor.executar(cfg or cfg_teste(), historico or [], ColetorFalso(respostas), self.AGORA)

    def hist(self, novas):
        return [dict(l) for l in novas]

    def test_link_vazio_e_ignorado(self):
        c = ColetorFalso({"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 1200})
        monitor.executar(cfg_teste(), [], c, self.AGORA)
        self.assertEqual(len(c.chamadas_direto), 3)

    def test_alvo_dispara_uma_vez(self):
        r1 = {"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 1200}
        novas1, _, alerta1, _ = self.rodar(r1)
        self.assertFalse(alerta1)
        r2 = dict(r1, **{"https://loja1.com/a": 880})
        novas2, blocos2, alerta2, _ = self.rodar(r2, self.hist(novas1))
        self.assertTrue(alerta2)
        self.assertIn("🎯", "\n".join(blocos2))
        _, _, alerta3, _ = self.rodar(r2, self.hist(novas1 + novas2))
        self.assertFalse(alerta3, "não deve repetir enquanto continua abaixo do alvo")

    def test_alvo_dispara_de_novo_apos_subir(self):
        h = []
        for preco in (880, 950):
            novas, *_ = self.rodar({"https://loja1.com/a": preco}, h)
            h += novas
        _, blocos, alerta, _ = self.rodar({"https://loja1.com/a": 870}, h)
        self.assertTrue(alerta)
        self.assertIn("🎯", "\n".join(blocos))

    def test_queda_percentual(self):
        novas, *_ = self.rodar({"https://loja2.com/a": 1100})
        _, blocos, alerta, _ = self.rodar({"https://loja2.com/a": 1000}, self.hist(novas))
        self.assertTrue(alerta)
        self.assertIn("📉", "\n".join(blocos))

    def test_queda_pequena_nao_alerta(self):
        novas, *_ = self.rodar({"https://loja2.com/a": 1100})
        _, _, alerta, mudou = self.rodar({"https://loja2.com/a": 1090}, self.hist(novas))
        self.assertFalse(alerta)
        self.assertTrue(mudou)

    def test_menor_preco_historico(self):
        h = []
        for preco in (1100, 1200):
            novas, *_ = self.rodar({"https://loja2.com/a": preco}, h)
            h += novas
        _, blocos, alerta, _ = self.rodar({"https://loja2.com/a": 1080}, h)
        self.assertTrue(alerta)
        self.assertIn("🏆", "\n".join(blocos))

    def test_esgotado_nao_alerta(self):
        _, _, alerta, _ = self.rodar({"https://loja1.com/a": (500,)})
        self.assertFalse(alerta)

    def test_esgotado_sem_preco_nao_grava(self):
        novas, blocos, _, _ = self.rodar({"https://loja1.com/a": "esgotado", "https://loja2.com/a": 1000})
        self.assertEqual([n["loja"] for n in novas], ["Loja2"])

    def test_sem_mudanca(self):
        r = {"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 1200}
        novas, *_ = self.rodar(r)
        _, _, alerta, mudou = self.rodar(r, self.hist(novas))
        self.assertFalse(alerta)
        self.assertFalse(mudou)

    def test_deve_enviar(self):
        d = monitor.deve_enviar
        self.assertTrue(d("se_mudar", False, True, False))
        self.assertFalse(d("se_mudar", False, False, False))
        self.assertTrue(d("se_mudar", True, False, False))
        self.assertTrue(d("so_alertas", False, False, True))
        self.assertFalse(d("so_alertas", False, True, False))
        self.assertTrue(d("sempre", False, False, False))


# ------------------------------------------------------------------ mensagem
class TestMensagem(unittest.TestCase):
    def setUp(self):
        r = {"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 950}
        cfg = cfg_teste()
        cfg["produtos"][0]["ofertas"] += [{"loja": f"Extra{i}", "url": f"https://extra{i}.com/a"} for i in range(4)]
        cfg["produtos"].append({"nome": "Modelo C", "faixa_valida": FAIXA,
                                "ofertas": [{"loja": "Fora", "url": "https://fora.com/c"}]})
        r.update({f"https://extra{i}.com/a": 1200 + i for i in range(4)})
        r["https://fora.com/c"] = "esgotado"
        cfg["produtos"].append({"nome": "Modelo D", "faixa_valida": FAIXA,
                                "ofertas": [{"loja": "Bloq", "url": "https://bloq.com/d"}]})
        _, self.blocos, _, _ = monitor.executar(cfg, [], ColetorFalso(r), "2026-10-30 14:05")
        self.txt = "\n".join(self.blocos)

    def test_ordem_e_medalhas(self):
        self.assertLess(self.txt.index("Modelo B"), self.txt.index("Modelo A"))
        self.assertIn("🥇 <b>Modelo B</b>", self.txt)
        self.assertIn("🥈 <b>Modelo A</b>", self.txt)

    def test_linha_secundaria_limitada(self):
        self.assertIn("+2", self.txt)  # 6 ofertas: 1 destaque + 3 listadas + "+2"
        self.assertIn("(+50)", self.txt)

    def test_sem_estoque_e_falhas(self):
        self.assertIn("⛔ <b>Modelo C</b>", self.txt)
        self.assertIn("Sem leitura: Bloq", self.txt)

    def test_cabecalho(self):
        self.assertTrue(self.blocos[0].startswith("📊 <b>Teste</b> · 30/10 14:05"))

    def test_html_balanceado(self):
        for tag in ("b", "i", "a"):
            self.assertEqual(self.txt.count(f"<{tag}>") + self.txt.count(f"<{tag} "), self.txt.count(f"</{tag}>"), tag)

    def test_compacta(self):
        self.assertLess(len(monitor.texto_visivel(self.txt).splitlines()), 15)

    def test_divide_mensagens_longas(self):
        blocos = ["x" * 2000, "y" * 2000, "z" * 2000]
        msgs = monitor.dividir_mensagem(blocos)
        self.assertEqual(len(msgs), 3)
        self.assertTrue(all(len(m) <= monitor.LIMITE_TELEGRAM for m in msgs))
        self.assertEqual(len(monitor.dividir_mensagem(["a", "b"])), 1)


# ------------------------------------------------------------------ telegram e main
class TestTelegramEMain(unittest.TestCase):
    def test_telegram_silencioso_sem_alerta(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             mock.patch.object(monitor.requests, "post") as post:
            post.return_value.status_code = 200
            self.assertTrue(monitor.telegram("oi", silencioso=True))
            self.assertEqual(post.call_args.kwargs["data"]["disable_notification"], "true")
            self.assertEqual(post.call_args.kwargs["data"]["parse_mode"], "HTML")

    def test_telegram_sem_configuracao(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(monitor.telegram("oi"))

    def test_main_ponta_a_ponta(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, hist_path = Path(d, "config.json"), Path(d, "historico.csv")
            cfg_path.write_text(json.dumps(cfg_teste()), encoding="utf-8")
            r = {"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 1200}
            env = {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ENVIAR_RESUMO": ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(monitor.requests, "post") as post:
                post.return_value.status_code = 200
                res1 = monitor.main(cfg_path, hist_path, ColetorFalso(r))
                self.assertTrue(res1["enviado"])                          # 1ª execução: tudo é novo
                res2 = monitor.main(cfg_path, hist_path, ColetorFalso(r))
                self.assertFalse(res2["enviado"])                         # nada mudou: não envia
                res3 = monitor.main(cfg_path, hist_path, ColetorFalso(dict(r, **{"https://loja1.com/b": 850})))
                self.assertTrue(res3["alerta"] and res3["enviado"])        # alvo: envia com som
                self.assertEqual(post.call_args.kwargs["data"]["disable_notification"], "false")
            linhas = monitor.ler_historico(hist_path)
            self.assertEqual(len(linhas), 9)

    def test_envio_manual_forcado(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, hist_path = Path(d, "config.json"), Path(d, "historico.csv")
            cfg_path.write_text(json.dumps(cfg_teste()), encoding="utf-8")
            r = {"https://loja1.com/a": 1000}
            with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                 mock.patch.object(monitor.requests, "post") as post:
                post.return_value.status_code = 200
                monitor.main(cfg_path, hist_path, ColetorFalso(r))
                with mock.patch.dict(os.environ, {"ENVIAR_RESUMO": "true"}):
                    self.assertTrue(monitor.main(cfg_path, hist_path, ColetorFalso(r))["enviado"])


# ------------------------------------------------------------------ config real
class TestConfigReal(unittest.TestCase):
    def test_config_valido(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        self.assertIn(cfg.get("enviar_resumo", "se_mudar"), ("sempre", "se_mudar", "so_alertas"))
        urls = []
        for p in cfg["produtos"]:
            self.assertTrue(p["nome"])
            lo, hi = p["faixa_valida"]
            self.assertLess(lo, hi)
            if p.get("preco_alvo"):
                self.assertTrue(lo <= p["preco_alvo"] <= hi, p["nome"])
            for o in p["ofertas"]:
                self.assertTrue(o["loja"])
                if o["url"]:
                    self.assertTrue(o["url"].startswith("https://"), o["url"])
                    urls.append(o["url"])
        self.assertEqual(len(urls), len(set(urls)), "há links duplicados")
        self.assertGreater(len(urls), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
