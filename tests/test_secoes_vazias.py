"""Cenário real da 1ª consulta após atualizar: 500GB ainda sem links. A estrutura tem que aparecer."""
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

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
AGORA = "2026-11-23 13:36"


def mensagem_primeira_consulta():
    """Reproduz o print de 23/11 13:36: só 3 modelos de 1TB com preço, 3 sem estoque, 500GB sem links."""
    cfg = json.loads(json.dumps(CFG))
    cfg["descoberta"]["ativa"] = False
    precos = {"Kingston XS1000": 1039.99, "Kingston XS2000": 1999.99, "Samsung T7 Shield": 2499.98}
    esgotados = {"Samsung T7", "SanDisk Extreme", "Crucial X9"}
    resp = {}
    for i, p in enumerate(cfg["produtos"]):
        if p["capacidade_gb"] == 500:
            p["ofertas"] = []                       # ainda não descobertos
            continue
        url = f"https://www.kabum.com.br/produto/{i}/x"
        p["ofertas"] = [{"loja": "KaBuM!", "url": url}] if p["nome"] in precos or p["nome"] in esgotados else []
        if p["nome"] in precos:
            resp[url] = pagina_produto(p["nome"], precos[p["nome"]])
        elif p["nome"] in esgotados:
            resp[url] = (999,)                      # esgotado
    extras = {}
    _, blocos, _, _ = monitor.executar(cfg, [], ColetorFalso(resp), AGORA, {}, extras)
    return monitor.texto_visivel("\n\n".join(blocos)), extras["botoes"]


class TestPrimeiraConsulta(unittest.TestCase):
    def test_estrutura_completa_mesmo_sem_500gb(self):
        txt, _ = mensagem_primeira_consulta()
        self.assertIn("💾 1TB\n1. Kingston XS1000 · R$ 1.040 · KaBuM!", txt)
        self.assertIn("💾 500GB\nSem preço nesta rodada — buscando os links.", txt)
        self.assertLess(txt.index("💾 1TB"), txt.index("💾 500GB"))
        self.assertLess(txt.index("💾 500GB"), txt.index("MELHOR CUSTO-BENEFÍCIO"))
        self.assertIn("Kingston XS1000 1TB — R$ 1.039,99 na KaBuM!", txt)
        self.assertIn("Sem os dois tamanhos do mesmo modelo com preço nesta rodada.", txt)

    def test_sem_estoque_fica_na_secao_certa(self):
        txt, _ = mensagem_primeira_consulta()
        bloco_1tb = txt[txt.index("💾 1TB"):txt.index("💾 500GB")]
        self.assertIn("Sem estoque: Samsung T7, SanDisk Extreme, Crucial X9", bloco_1tb)

    def test_botoes_continuam(self):
        _, botoes = mensagem_primeira_consulta()
        self.assertEqual([b[0]["text"] for b in botoes][-3:], ["1. Kingston XS1000 1TB · R$ 1.040 · KaBuM!",
                                                               "2. Kingston XS2000 1TB · R$ 2.000 · KaBuM!",
                                                               "3. Samsung T7 Shield 1TB · R$ 2.500 · KaBuM!"])

    def test_nenhum_preco_ainda(self):
        res = [{"nome": "A", "rotulo": "A 1TB", "cap": 1000, "ofertas": []},
               {"nome": "B", "rotulo": "B 500GB", "cap": 500, "ofertas": []}]
        txt = monitor.texto_visivel("\n\n".join(monitor.montar_mensagem("T", AGORA, res, [], [], 0, "numeros")))
        self.assertEqual(txt.count("buscando os links"), 2)
        self.assertIn("Aguardando os primeiros preços.", txt)


class TestPrioridadeDaBusca(unittest.TestCase):
    def test_depois_de_zerar_busca_primeiro_500gb_e_pares(self):
        ordem = []
        estado = {}
        hora = 13
        while len(ordem) < len(CFG["produtos"]):
            agora = f"2026-11-23 {hora:02d}:07"
            lote = monitor.modelos_a_descobrir(CFG, estado, agora)
            for p in lote:
                estado.setdefault("descobertos", {})[p["nome"]] = {"atualizado": agora, "ofertas": []}
            ordem += [p["nome"] for p in lote]
            hora += 1
        pares = {p["par_1tb"] for p in CFG["produtos"] if p.get("par_1tb")}
        do_comparativo = [p["nome"] for p in CFG["produtos"] if p["capacidade_gb"] == 500 or p["nome"] in pares]
        primeiros = ordem[:len(do_comparativo)]
        self.assertEqual(set(primeiros), set(do_comparativo))
        n = CFG["descoberta"]["modelos_por_execucao"]
        self.assertLessEqual(len(do_comparativo) / n, 3)   # comparativo completo em até 3 consultas


if __name__ == "__main__":
    unittest.main(verbosity=2)
