"""Testes da ampliação de lojas: pausa automática de lojas que bloqueiam e busca genérica."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import monitor  # noqa: E402
from test_descoberta import pagina_busca, pagina_produto  # noqa: E402
from test_monitor import ColetorFalso  # noqa: E402

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
P = {p["nome"]: p for p in CFG["produtos"]}
LOJAS = {l["loja"]: l for l in CFG["lojas_busca"]}


def cfg_teste():
    return {"titulo": "T", "estilo_ranking": "numeros", "pausa_apos_falhas": 3, "pausa_loja_horas": 24,
            "enviar_resumo": "sempre", "produtos": [
                {"nome": "M", "capacidade_gb": 1000, "faixa_valida": [300, 4000], "ofertas": [
                    {"loja": "Boa", "url": "https://boa.com/1"},
                    {"loja": "Bloqueia", "url": "https://bloq.com/1"}]}]}


class TestPausaAutomatica(unittest.TestCase):
    def setUp(self):
        original = monitor.agora_br
        self.addCleanup(setattr, monitor, "agora_br", original)

    def rodar(self, d, hora, respostas):
        monitor.agora_br = lambda: f"2026-11-23 {hora:02d}:07"
        cfg_path = Path(d, "config.json")
        cfg_path.write_text(json.dumps(cfg_teste()), encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            return monitor.main(cfg_path, Path(d, "historico.csv"), ColetorFalso(respostas, usar_leitor=False))

    def test_ciclo_completo(self):
        with tempfile.TemporaryDirectory() as d:
            r = {"https://boa.com/1": 900}                       # bloq.com sempre 403
            for h in (10, 11):
                res = self.rodar(d, h, r)
                self.assertIn("Sem leitura: Bloqueia", monitor.texto_visivel("\n".join(res["blocos"])))
            res = self.rodar(d, 12, r)                            # 3ª falha seguida -> pausa
            lojas = res["estado"]["lojas"]
            self.assertEqual(lojas["bloq.com"]["pausa_ate"], "2026-11-24 12:07")
            self.assertNotIn("pausa_ate", lojas["boa.com"])

            c = ColetorFalso(r, usar_leitor=False)                # durante a pausa: nem tenta
            monitor.agora_br = lambda: "2026-11-23 13:07"
            cfg_path = Path(d, "config.json")
            with mock.patch.dict("os.environ", {}, clear=True):
                res = monitor.main(cfg_path, Path(d, "historico.csv"), c)
            self.assertNotIn("https://bloq.com/1", c.chamadas_direto)
            txt = monitor.texto_visivel("\n".join(res["blocos"]))
            self.assertIn("ℹ️ Em pausa: Bloqueia", txt)
            self.assertNotIn("Sem leitura", txt)

    def test_volta_da_pausa_e_se_bloquear_pausa_de_novo_na_hora(self):
        estado = {"lojas": {"bloq.com": {"falhas_seguidas": 0, "reincidente": True}}}
        c = ColetorFalso({}, usar_leitor=False)
        c.coletar("https://bloq.com/1", [300, 4000])
        novas = monitor.atualizar_pausas({}, estado, c, "2026-11-24 12:07")
        self.assertEqual(novas, ["bloq.com"])
        self.assertEqual(estado["lojas"]["bloq.com"]["pausa_ate"], "2026-11-25 12:07")

    def test_loja_que_voltou_a_funcionar_sai_da_pausa(self):
        estado = {"lojas": {"boa.com": {"falhas_seguidas": 2, "reincidente": True}}}
        c = ColetorFalso({"https://boa.com/1": 900}, usar_leitor=False)
        c.coletar("https://boa.com/1", [300, 4000])
        monitor.atualizar_pausas({}, estado, c, "2026-11-24 12:07")
        self.assertEqual(estado["lojas"]["boa.com"], {"falhas_seguidas": 0, "reincidente": False})

    def test_pagina_sem_preco_nao_conta_como_bloqueio(self):
        estado = {}
        c = ColetorFalso({"https://ok.com/1": pagina_busca("/x")}, usar_leitor=False)
        c.coletar("https://ok.com/1", [300, 4000])
        monitor.atualizar_pausas({}, estado, c, "2026-11-24 12:07")
        self.assertEqual(estado.get("lojas", {}), {})

    def test_pausa_ainda_valida_e_vencida(self):
        estado = {"lojas": {"a.com": {"pausa_ate": "2026-11-24 12:07"}, "b.com": {"pausa_ate": "2026-11-23 09:00"}}}
        c = ColetorFalso({})
        self.assertEqual(monitor.aplicar_pausas(estado, c, "2026-11-23 13:07"), ["a.com"])


class TestBuscaGenerica(unittest.TestCase):
    LOJA = {"loja": "X", "base": "https://www.lojax.com.br", "padrao": "generico"}

    def test_acha_produto_e_ignora_menus_e_busca(self):
        pagina = pagina_busca("/busca/samsung-t7-1tb", "/categoria/ssd-externo-samsung-t7-1tb",
                              "https://www.lojax.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p",
                              "https://outrosite.com/ssd-samsung-t7-1tb", "/ssd-samsung-t9-1tb/p",
                              "/conta/login", "/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p")
        self.assertEqual(monitor.extrair_candidatos(pagina, self.LOJA, P["Samsung T7"]),
                         ["https://www.lojax.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p"])

    def test_lojas_novas_reconhecem_links(self):
        casos = {
            "Samsung Shop": ("/br/samsung-ssd-t7-titan-1tb/p", "https://shop.samsung.com/br/samsung-ssd-t7-titan-1tb/p"),
            "Submarino": ("/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p",
                          "https://www.submarino.com.br/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p"),
            "Shoptime": ("/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p",
                         "https://www.shoptime.com.br/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p"),
            "Extra": ("/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555",
                      "https://www.extra.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555"),
            "Ponto": ("/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555",
                      "https://www.pontofrio.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555"),
            "Fast Shop": ("/web/p/d/SAMSUNGMUPC1T0T_PRD/ssd-portatil-samsung-t7-1tb",
                          "https://www.fastshop.com.br/web/p/d/SAMSUNGMUPC1T0T_PRD/ssd-portatil-samsung-t7-1tb"),
            "Kalunga": ("/prod/ssd-externo-samsung-t7-1tb-mu-pc1t0t/123456",
                        "https://www.kalunga.com/prod/ssd-externo-samsung-t7-1tb-mu-pc1t0t/123456"),
            "Girafa": ("/informatica/ssd-externo-samsung-t7-1tb-mu-pc1t0t.htm",
                       "https://www.girafa.com.br/informatica/ssd-externo-samsung-t7-1tb-mu-pc1t0t.htm"),
        }
        for loja, (href, esperado) in casos.items():
            with self.subTest(loja=loja):
                self.assertEqual(monitor.extrair_candidatos(pagina_busca(href), LOJAS[loja], P["Samsung T7"]),
                                 [esperado])

    def test_samsung_shop_so_para_samsung(self):
        lojas = [l["loja"] for l in monitor.lojas_para(P["Kingston XS1000"], CFG["lojas_busca"])]
        self.assertNotIn("Samsung Shop", lojas)
        self.assertIn("Samsung Shop", [l["loja"] for l in monitor.lojas_para(P["Samsung T7"], CFG["lojas_busca"])])


class TestTempoLimite(unittest.TestCase):
    def test_site_que_trava_nao_e_tentado_duas_vezes(self):
        chamadas = []

        class Sessao:
            def get(self, *a, **k):
                chamadas.append(1)
                raise Exception("Operation timed out after 20000 milliseconds")

        falso = type("N", (), {"Session": staticmethod(lambda impersonate: Sessao())})
        with mock.patch.object(monitor, "navegador", falso):
            pagina, erro = monitor.Coletor().baixar("https://trava.com/1")
        self.assertIsNone(pagina)
        self.assertIn("timed out", erro)
        self.assertEqual(len(chamadas), 1)


class TestConfigLojas(unittest.TestCase):
    def test_lojas_ampliadas(self):
        nomes = {l["loja"] for l in CFG["lojas_busca"]}
        for n in ("KaBuM!", "Amazon", "Terabyte", "Pichau", "Kingston Store", "Mercado Livre", "Magalu",
                  "Casas Bahia", "Carrefour", "Americanas", "Samsung Shop", "Fast Shop", "Kalunga", "Girafa",
                  "Submarino", "Shoptime", "Extra", "Ponto"):
            self.assertIn(n, nomes)
        self.assertFalse(any(l.get("ativo") is False for l in CFG["lojas_busca"]))
        self.assertEqual((CFG["pausa_apos_falhas"], CFG["pausa_loja_horas"]), (3, 24))

    def test_e10_tem_par_exato(self):
        self.assertEqual(P["SanDisk Portable E10 500GB"]["par_1tb"], "SanDisk Portable E10")
        self.assertNotIn("par_aproximado", P["SanDisk Portable E10 500GB"])
        self.assertEqual(P["SanDisk Portable E10"]["capacidade_gb"], 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
