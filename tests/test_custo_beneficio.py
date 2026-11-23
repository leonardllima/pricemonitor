"""Testes dos modelos de 500GB e do comparativo de custo-benefício com o 1TB (offline)."""
import json
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import monitor  # noqa: E402
from test_descoberta import pagina_produto  # noqa: E402
from test_monitor import ColetorFalso  # noqa: E402
from test_portatil import PORTATEIS  # noqa: E402

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
P = {p["nome"]: p for p in CFG["produtos"]}

TITULOS_NOVOS = {
    "Samsung T7 500GB": "SSD Externo Samsung T7 500GB USB 3.2 Titan Gray MU-PC500T/WW",
    "SanDisk Portable E10 500GB": "SSD Portátil SanDisk 500GB USB-C 600MB/s SDSSDE10-500G-G25",
    "SanDisk Portable V3 500GB": "SSD Portátil SanDisk V3 500GB USB-C 1000MB/s SDSSDE31-500G-G25",
    "SanDisk Extreme 500GB": "SSD Externo Portátil SanDisk Extreme 500GB USB-C SDSSDE61-500G-G25",
    "SanDisk Extreme V3 500GB": "SSD Portátil SanDisk Extreme V3 500GB USB-C 2000MB/s SDSSDE70-500G-G25",
    "Kingston XS2000 500GB": "SSD Externo Kingston XS2000 500GB USB-C 3.2 Gen 2x2 SXS2000/500G",
    "SanDisk Portable E10": "SSD Externo Portátil Sandisk, 1TB, USB-C, Leitura 600MB/s, SDSSDE10-1T00-G25",
    "SanDisk Portable V3": "SSD Portátil SanDisk V3 1TB USB-C 1000MB/s SDSSDE31-1T00-G25",
    "SanDisk Extreme V3": "SSD Portátil SanDisk Extreme V3 1TB USB-C 2000MB/s SDSSDE70-1T00-G25",
}
TODOS = {**PORTATEIS, **TITULOS_NOVOS}


def item(nome, preco, cap=None, par=None, vel=None, loja="Loja", aprox=False):
    return {"nome": nome, "rotulo": monitor.rotulo_produto({"nome": nome, "capacidade_gb": cap}),
            "cap": cap, "par": par, "vel": vel, "par_aproximado": aprox,
            "ofertas": [{"loja": loja, "url": f"https://x/{nome.replace(' ', '-')}", "preco": preco,
                         "disp": True, "ultimo": None}]}


# ------------------------------------------------------------------ configuração
class TestConfig(unittest.TestCase):
    def test_todos_os_modelos_tem_capacidade_e_velocidade(self):
        for p in CFG["produtos"]:
            with self.subTest(p["nome"]):
                self.assertIn(p["capacidade_gb"], (500, 1000))
                self.assertGreater(p["leitura_mbs"], 0)

    def test_os_6_modelos_de_500gb_estao_na_busca(self):
        codigos = {c for p in CFG["produtos"] for c in p["codigos"]}
        for c in ("MU-PC500T", "SDSSDE10-500G", "SDSSDE31-500G", "SDSSDE61-500G", "SDSSDE70-500G", "SXS2000/500G"):
            self.assertIn(c, codigos)

    def test_cada_500gb_aponta_para_um_1tb_existente(self):
        for p in CFG["produtos"]:
            if p["capacidade_gb"] == 500:
                with self.subTest(p["nome"]):
                    self.assertIn(p["par_1tb"], P)
                    self.assertEqual(P[p["par_1tb"]]["capacidade_gb"], 1000)

    def test_versao_das_regras_foi_atualizada(self):
        self.assertGreaterEqual(CFG["descoberta"]["versao"], 3)


# ------------------------------------------------------------------ identificação
class TestIdentificacao(unittest.TestCase):
    def test_cada_titulo_so_casa_com_o_proprio_modelo(self):
        for nome_titulo, t in TODOS.items():
            pagina = pagina_produto(t, 600)
            for nome_modelo, prod in P.items():
                with self.subTest(titulo=nome_titulo, modelo=nome_modelo):
                    self.assertEqual(monitor.produto_confere(pagina, prod), nome_titulo == nome_modelo)

    def test_linhas_parecidas_exigem_codigo(self):
        sem_codigo = pagina_produto("SSD Portátil SanDisk 500GB USB-C", 400)
        for nome in ("SanDisk Portable E10 500GB", "SanDisk Portable V3 500GB"):
            self.assertFalse(monitor.produto_confere(sem_codigo, P[nome]))
        self.assertFalse(monitor.produto_confere(pagina_produto("SSD Externo SanDisk Extreme 1TB USB-C", 900),
                                                 P["SanDisk Extreme V3"]))

    def test_500gb_nao_entra_no_1tb_e_vice_versa(self):
        self.assertFalse(monitor.candidato_ok("/produto/1/ssd-samsung-t7-500gb-mu-pc500t", P["Samsung T7"]))
        self.assertFalse(monitor.candidato_ok("/produto/1/ssd-samsung-t7-1tb-mu-pc1t0t", P["Samsung T7 500GB"]))
        self.assertTrue(monitor.candidato_ok("/produto/1/ssd-samsung-t7-500gb-mu-pc500t", P["Samsung T7 500GB"]))


# ------------------------------------------------------------------ rótulos e seções
class TestRotulos(unittest.TestCase):
    def test_rotulo(self):
        self.assertEqual(monitor.rotulo_produto({"nome": "Samsung T7", "capacidade_gb": 1000}), "Samsung T7 1TB")
        self.assertEqual(monitor.rotulo_produto({"nome": "Samsung T7 500GB", "capacidade_gb": 500}), "Samsung T7 500GB")
        self.assertEqual(monitor.rotulo_produto({"nome": "Modelo A"}), "Modelo A")

    def test_mensagem_separa_1tb_e_500gb_com_medalhas_proprias(self):
        res = [item("A", 1000, 1000, vel=1050), item("B", 900, 1000), item("A 500GB", 600, 500, par="A"),
               item("C 500GB", 400, 500)]
        txt = monitor.texto_visivel("\n\n".join(monitor.montar_mensagem("T", "2026-11-23 13:07", res, [], [])))
        self.assertLess(txt.index("💾 1TB"), txt.index("💾 500GB"))
        self.assertIn("💾 1TB\n🥇 B · R$ 900 · Loja\n🥈 A · R$ 1.000 · Loja", txt)
        self.assertIn("💾 500GB\n🥇 C · R$ 400 · Loja\n🥈 A · R$ 600 · Loja", txt)

    def test_botoes_por_secao(self):
        res = [item("A", 1000, 1000), item("A 500GB", 600, 500, par="A")]
        botoes = monitor.montar_botoes(res, [])
        self.assertEqual([b[0]["text"] for b in botoes], ["🥇 A 1TB · R$ 1.000 · Loja", "🥇 A 500GB · R$ 600 · Loja"])

    def test_sem_capacidade_mantem_formato_antigo(self):
        res = [item("A", 1000), item("B", 900)]
        txt = "\n".join(monitor.montar_mensagem("T", "2026-11-23 13:07", res, [], []))
        self.assertNotIn("💾", txt)
        self.assertNotIn("CUSTO-BENEFÍCIO", txt)
        self.assertNotIn("500GB OU 1TB", txt)


# ------------------------------------------------------------------ custo-benefício
class TestCustoBeneficio(unittest.TestCase):
    def cb(self, *itens):
        com_preco, _ = monitor.ranking_modelos(list(itens))
        blocos, resumo = monitor.custo_beneficio(com_preco)
        return monitor.texto_visivel("\n\n".join(blocos)), resumo

    def test_destaque_e_explicacao(self):
        # 500GB a 700 = 1.400/TB ; 1TB a 1.000 = 1.000/TB
        txt, r = self.cb(item("T7", 1000, 1000, vel=1050), item("T7 500GB", 700, 500, par="T7", vel=1050))
        self.assertTrue(txt.startswith("⭐ MELHOR CUSTO-BENEFÍCIO DESTA RODADA\nT7 1TB — R$ 1.000,00 na Loja"))
        self.assertIn("✔️ É o que dá mais espaço por real: R$ 1.000 por TB (o 2º, T7 500GB, sai R$ 1.400/TB).", txt)
        self.assertIn("✔️ Velocidade de 1.050 MB/s — boa para backup e uso diário.", txt)
        self.assertIn("💰 Quer gastar menos? T7 500GB por R$ 700 na Loja — mas paga 40% a mais por TB.", txt)
        self.assertEqual((r["melhor"], r["barato"]), ("T7", "T7 500GB"))

    def test_quando_o_melhor_tambem_e_o_mais_barato(self):
        txt, r = self.cb(item("X", 1300, 1000), item("X 500GB", 600, 500, par="X"))
        self.assertEqual(r["melhor"], "X 500GB")
        self.assertIn("✔️ E ainda é o mais barato da rodada.", txt)
        self.assertNotIn("💰", txt)

    def test_500gb_ou_1tb(self):
        txt, _ = self.cb(item("A", 1000, 1000), item("A 500GB", 700, 500, par="A"),     # 1TB 29% menos/TB
                         item("B", 1300, 1000), item("B 500GB", 600, 500, par="B"),     # 1TB > 2x
                         item("C", 1100, 1000), item("C 500GB", 600, 500, par="C"))     # meio-termo
        self.assertIn("⚖️ 500GB OU 1TB? quanto custa a mais o dobro de espaço", txt)
        self.assertIn("• A: +R$ 300 (+43%) → ✅ vale o 1TB", txt)
        self.assertIn("• B: +R$ 700 (+117%) → ✅ vale o 500GB", txt)
        self.assertIn("• C: +R$ 500 (+83%) → ➖ tanto faz", txt)

    def test_niveis_de_velocidade(self):
        for vel, texto in ((600, "mais simples, suficiente para backup e arquivos"),
                           (1050, "boa para backup e uso diário"), (2000, "das mais rápidas")):
            with self.subTest(vel=vel):
                txt, _ = self.cb(item("V", 900, 1000, vel=vel), item("W 500GB", 800, 500))
                self.assertIn(texto, txt)

    def test_rapido(self):
        txt, r = self.cb(item("Lento", 900, 1000, vel=1050), item("Rapido", 1200, 1000, vel=2000),
                         item("Rapido 500GB", 700, 500, par="Rapido", vel=2000))
        self.assertEqual(r["rapido"], "Rapido")
        self.assertIn("⚡ Precisa de velocidade? Rapido 1TB (2.000 MB/s) por R$ 1.200 na Loja", txt)

    def test_sem_pares(self):
        txt, r = self.cb(item("Z", 1000, 1000), item("W 500GB", 500, 500, par="Inexistente"))
        self.assertIn("Sem os dois tamanhos do mesmo modelo com preço nesta rodada.", txt)
        self.assertEqual(r["pares"], 0)
        self.assertIn("⭐", txt)

    def test_texto_curto(self):
        txt, _ = self.cb(item("A", 1000, 1000, vel=1050), item("A 500GB", 700, 500, par="A", vel=1050),
                         item("F", 1500, 1000, vel=2000))
        self.assertLessEqual(len(txt.splitlines()), 10)


# ------------------------------------------------------------------ ponta a ponta com a config real
class TestPontaAPonta(unittest.TestCase):
    def test_todos_os_modelos_mensagem_completa_e_dentro_do_limite(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["descoberta"]["ativa"] = False
        precos = {}
        for i, p in enumerate(cfg["produtos"]):
            url = f"https://www.kabum.com.br/produto/{i}/x"
            p["ofertas"] = [{"loja": "KaBuM!", "url": url}]
            precos[url] = pagina_produto(TODOS[p["nome"]], (450 if p["capacidade_gb"] == 500 else 900) + 10 * i)
        extras = {}
        _, blocos, _, _ = monitor.executar(cfg, [], ColetorFalso(precos), "2026-11-23 13:07", {}, extras)
        txt = monitor.texto_visivel("\n\n".join(blocos))
        self.assertIn("💾 1TB", txt)
        self.assertIn("💾 500GB", txt)
        self.assertIn("⭐ MELHOR CUSTO-BENEFÍCIO DESTA RODADA", txt)
        self.assertEqual(txt.count(" → ✅") + txt.count(" → ➖"), 6)   # 6 pares 500GB × 1TB
        ranking = [b for b in extras["botoes"] if b[0]["text"][:1] in "🥇🥈🥉" or b[0]["text"][0].isdigit()]
        self.assertEqual(len(ranking), len(CFG["produtos"]))   # 1 botão por modelo (+ botões de alerta no topo)
        self.assertLessEqual(len(monitor.dividir_mensagem(blocos)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
