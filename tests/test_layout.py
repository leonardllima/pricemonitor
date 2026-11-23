"""Testes do layout por capacidade (1TB / 500GB numerados), da faixa-alvo e dos botões."""
import json
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import monitor  # noqa: E402
from test_custo_beneficio import item  # noqa: E402
from test_monitor import ColetorFalso  # noqa: E402

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
AGORA = "2026-11-23 13:07"


def mensagem(res, outras=0):
    blocos = monitor.montar_mensagem("SSDs portáteis", AGORA, res, [], [], outras, "numeros")
    return monitor.texto_visivel("\n\n".join(blocos))


RES = [item("A", 1000, 1000, vel=1050), item("B", 900, 1000), item("C", 1100, 1000),
       item("A 500GB", 600, 500, par="A"), item("D 500GB", 450, 500), item("E 500GB", 700, 500)]


class TestLayout(unittest.TestCase):
    def test_marcador(self):
        self.assertEqual([monitor.marcador(i, "numeros") for i in range(4)], ["1.", "2.", "3.", "4."])
        self.assertEqual(monitor.marcador(0), "🥇")

    def test_secoes_numeradas_e_recomendacao_depois(self):
        txt = mensagem(RES)
        self.assertIn("💾 1TB\n1. B · R$ 900 · Loja\n2. A · R$ 1.000 · Loja\n3. C · R$ 1.100 · Loja", txt)
        self.assertIn("💾 500GB\n1. D · R$ 450 · Loja\n2. A · R$ 600 · Loja\n3. E · R$ 700 · Loja", txt)
        self.assertLess(txt.index("💾 1TB"), txt.index("💾 500GB"))
        self.assertLess(txt.index("💾 500GB"), txt.index("MELHOR CUSTO-BENEFÍCIO"))
        self.assertLess(txt.index("MELHOR CUSTO-BENEFÍCIO"), txt.index("500GB OU 1TB?"))
        self.assertNotIn("🥇", txt)

    def test_sem_lojas_alternativas(self):
        self.assertNotIn("↳", mensagem(RES, outras=0))

    def test_botoes_numerados_por_secao(self):
        textos = [b[0]["text"] for b in monitor.montar_botoes(RES, [], 0, "numeros")]
        self.assertEqual(textos, ["1. B 1TB · R$ 900 · Loja", "2. A 1TB · R$ 1.000 · Loja",
                                  "3. C 1TB · R$ 1.100 · Loja", "1. D 500GB · R$ 450 · Loja",
                                  "2. A 500GB · R$ 600 · Loja", "3. E 500GB · R$ 700 · Loja"])


class TestFaixaAlvo(unittest.TestCase):
    def cfg(self):
        return {"titulo": "T", "estilo_ranking": "numeros", "produtos": [
            {"nome": "M", "capacidade_gb": 1000, "preco_alvo": 899, "preco_alvo_min": 799,
             "faixa_valida": [300, 4000], "ofertas": [{"loja": "L", "url": "https://l/1"}]}]}

    def rodar(self, preco, hist=None):
        extras = {}
        novas, blocos, alerta, _ = monitor.executar(self.cfg(), hist or [], ColetorFalso({"https://l/1": preco}),
                                                    AGORA, {}, extras)
        return novas, monitor.texto_visivel("\n".join(blocos)), alerta, extras["botoes"]

    def test_na_faixa(self):
        _, txt, alerta, botoes = self.rodar(850)
        self.assertTrue(alerta)
        self.assertIn("🎯 M 1TB · R$ 850 na L — na faixa 799–899", txt)
        self.assertTrue(botoes[0][0]["text"].startswith("🎯 M 1TB · R$ 850"))
        self.assertEqual(botoes[1][0]["text"], "1. M 1TB · R$ 850 · L")

    def test_abaixo_da_faixa(self):
        _, txt, alerta, _ = self.rodar(749)
        self.assertTrue(alerta)
        self.assertIn("🎯 M 1TB · R$ 749 na L — abaixo da faixa 799–899", txt)

    def test_acima_da_faixa_nao_alerta(self):
        self.assertFalse(self.rodar(950)[2])

    def test_nao_repete_enquanto_continua_na_faixa(self):
        novas, *_ = self.rodar(850)
        _, txt, _, _ = self.rodar(845, [dict(n) for n in novas])
        self.assertNotIn("🎯", txt)

    def test_volta_a_alertar_depois_de_sair_da_faixa(self):
        h = []
        for p in (850, 950):
            novas, *_ = self.rodar(p, h)
            h += [dict(n) for n in novas]
        self.assertTrue(self.rodar(880, h)[2])


class TestUmAlertaPorModelo(unittest.TestCase):
    def test_varias_lojas_viram_um_alerta_com_a_mais_barata(self):
        cfg = {"titulo": "T", "estilo_ranking": "numeros", "produtos": [
            {"nome": "M", "capacidade_gb": 500, "preco_alvo": 899, "preco_alvo_min": 799,
             "faixa_valida": [200, 3000], "ofertas": [{"loja": "L1", "url": "https://l/1"},
                                                      {"loja": "L2", "url": "https://l/2"}]}]}
        hist = [{"data_hora": "x", "produto": "M", "loja": "L1", "preco": "600.00", "disponivel": "1",
                 "url": "https://l/1"}]
        extras = {}
        _, blocos, alerta, _ = monitor.executar(cfg, hist, ColetorFalso({"https://l/1": 450, "https://l/2": 480}),
                                                AGORA, {}, extras)
        txt = monitor.texto_visivel("\n".join(blocos))
        self.assertTrue(alerta)
        self.assertEqual(txt.count(" na L"), 1)               # 1 linha de alerta só
        self.assertIn("M 500GB · R$ 450 na L1", txt)
        self.assertIn("🎯", txt)
        self.assertIn("📉", txt)
        self.assertEqual(sum(1 for b in extras["botoes"] if "450" in b[0]["text"] and b[0]["text"][0] != "1"), 1)


class TestConfigAtual(unittest.TestCase):
    def test_faixa_alvo_799_a_899_em_todos(self):
        for p in CFG["produtos"]:
            with self.subTest(p["nome"]):
                self.assertEqual((p["preco_alvo_min"], p["preco_alvo"]), (799, 899))

    def test_layout_configurado(self):
        self.assertEqual(CFG["estilo_ranking"], "numeros")
        self.assertEqual(CFG["outras_lojas_por_modelo"], 0)
        self.assertTrue(CFG["botoes"]["ativo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
