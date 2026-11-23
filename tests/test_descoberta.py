"""Testes da busca automática, validação de produto, intervalo e config (offline)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import intervalo  # noqa: E402
import monitor  # noqa: E402
from test_monitor import ColetorFalso  # noqa: E402

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
LOJAS = {l["loja"]: l for l in CFG["lojas_busca"]}
PRODUTOS = {p["nome"]: p for p in CFG["produtos"]}
T7, T7S, X9, X9P = (PRODUTOS[n] for n in ("Samsung T7", "Samsung T7 Shield", "Crucial X9", "Crucial X9 Pro"))


def pagina_produto(titulo, preco, extra=""):
    ld = {"@type": "Product", "name": titulo,
          "offers": {"@type": "Offer", "price": preco, "availability": "https://schema.org/InStock"}}
    return (f"<html><head><title>{titulo}</title><script type='application/ld+json'>{json.dumps(ld)}</script>"
            f"</head><body><h1>{titulo}</h1>{extra}</body></html>")


def pagina_busca(*hrefs, titulo="Resultado da busca"):
    links = "".join(f'<a href="{h}">item</a>' for h in hrefs)
    return f"<html><head><title>{titulo}</title></head><body>{links}</body></html>"


# ------------------------------------------------------------------ utilidades
class TestUtilidades(unittest.TestCase):
    def test_compacto(self):
        self.assertEqual(monitor.compacto("MU-PC1T0T/AM 1 TB"), "mupc1t0tam1tb")
        self.assertEqual(monitor.compacto("Portátil Ação"), "portatilacao")

    def test_url_de_busca(self):
        self.assertEqual(monitor.montar_url_busca("https://x.com/busca/{hifen}", "Samsung T7 1TB"),
                         "https://x.com/busca/samsung-t7-1tb")
        self.assertEqual(monitor.montar_url_busca("https://x.com/s?k={mais}", "samsung t7 1tb"),
                         "https://x.com/s?k=samsung+t7+1tb")

    def test_canonico_amazon(self):
        self.assertEqual(monitor.canonico("/Samsung-Portatil-T7-1TB/dp/B0874XN4D8/ref=sr_1_1",
                                          "https://www.amazon.com.br"), "https://www.amazon.com.br/dp/B0874XN4D8")

    def test_canonico_remove_query(self):
        self.assertEqual(monitor.canonico("/produto/1/ssd?origem=busca#x", "https://www.kabum.com.br"),
                         "https://www.kabum.com.br/produto/1/ssd")

    def test_termos_distinguem_modelos_parecidos(self):
        self.assertTrue(monitor.termos_ok("/produto/1/ssd-samsung-t7-1tb-mu-pc1t0t", T7))
        self.assertFalse(monitor.termos_ok("/produto/2/ssd-samsung-t7-shield-1tb", T7))
        self.assertFalse(monitor.termos_ok("/produto/3/ssd-samsung-t7-2tb", T7))
        self.assertTrue(monitor.termos_ok("/produto/2/ssd-samsung-t7-shield-1tb", T7S))
        self.assertTrue(monitor.termos_ok("/ssd-crucial-x9-1tb", X9))
        self.assertFalse(monitor.termos_ok("/ssd-crucial-x9-pro-1tb", X9))
        self.assertTrue(monitor.termos_ok("/ssd-crucial-x9-pro-1tb", X9P))

    def test_palavra_produto_nao_confunde_extreme_pro(self):
        sd = PRODUTOS["SanDisk Extreme"]
        self.assertTrue(monitor.termos_ok("/produto/129775/ssd-sandisk-extreme-1tb-portatil", sd))
        self.assertFalse(monitor.termos_ok("/produto/9/ssd-sandisk-extreme-pro-1tb", sd))

    def test_pagina_bloqueada(self):
        self.assertTrue(monitor.pagina_bloqueada("<title>Just a moment...</title>"))
        self.assertTrue(monitor.pagina_bloqueada("<title>Magazine Luiza | Não é possível acessar a página</title>"))
        self.assertTrue(monitor.pagina_bloqueada("<html><body></body></html>"))
        self.assertFalse(monitor.pagina_bloqueada(pagina_produto("SSD Samsung T7", 900)))


# ------------------------------------------------------------------ identidade
class TestProdutoConfere(unittest.TestCase):
    def test_codigo_no_titulo(self):
        self.assertTrue(monitor.produto_confere(pagina_produto("SSD Portátil Samsung MU-PC1T0T/AM", 900), T7))

    def test_codigo_so_em_relacionados_nao_vale(self):
        pagina = pagina_produto("SSD Portátil Samsung T7 Shield 1TB", 999,
                                extra="<div class='relacionados'>Veja também MU-PC1T0T/AM</div>")
        self.assertFalse(monitor.produto_confere(pagina, T7))
        self.assertTrue(monitor.produto_confere(pagina, T7S))

    def test_termos_no_titulo_sem_codigo(self):
        self.assertTrue(monitor.produto_confere(pagina_produto("SSD Externo Samsung T7 1TB USB 3.2", 900), T7))

    def test_modelo_errado(self):
        self.assertFalse(monitor.produto_confere(pagina_produto("SSD Samsung T9 1TB MU-PG1T0B", 1300), T7))

    def test_ficha_tecnica_amazon(self):
        pagina = ("<html><head><title>Amazon.com.br: SSD Portátil Samsung</title></head><body>"
                  "<span id='productTitle'>Samsung SSD Portátil</span><table id='productDetails_techSpec_section_1'>"
                  "<tr><th>Número do modelo</th><td>MU-PC1T0T/AM</td></tr></table></body></html>")
        self.assertTrue(monitor.produto_confere(pagina, T7))


# ------------------------------------------------------------------ páginas de busca de cada loja
class TestCandidatosPorLoja(unittest.TestCase):
    CASOS = {
        "KaBuM!": ('/produto/343491/ssd-externo-samsung-t7-1tb-mu-pc1t0t-am',
                   "https://www.kabum.com.br/produto/343491/ssd-externo-samsung-t7-1tb-mu-pc1t0t-am"),
        "Amazon": ('/Samsung-Port%C3%A1til-T7-1TB-Cinza/dp/B0874XN4D8/ref=sr_1_3?keywords=t7',
                   "https://www.amazon.com.br/dp/B0874XN4D8"),
        "Terabyte": ('/produto/99999/ssd-externo-samsung-t7-1tb-usb-32',
                     "https://www.terabyteshop.com.br/produto/99999/ssd-externo-samsung-t7-1tb-usb-32"),
        "Pichau": ('/ssd-externo-samsung-t7-1tb-usb-3-2-cinza-mu-pc1t0t-ww',
                   "https://www.pichau.com.br/ssd-externo-samsung-t7-1tb-usb-3-2-cinza-mu-pc1t0t-ww"),
        "Mercado Livre": ('https://www.mercadolivre.com.br/ssd-samsung-t7-1tb-cinza/p/MLB15999999',
                          "https://www.mercadolivre.com.br/ssd-samsung-t7-1tb-cinza/p/MLB15999999"),
        "Magalu": ('/ssd-externo-samsung-t7-1tb-usb-3-2-mu-pc1t0t-am/p/gj70gk153h/in/ssdi/',
                   "https://www.magazineluiza.com.br/ssd-externo-samsung-t7-1tb-usb-3-2-mu-pc1t0t-am/p/gj70gk153h/in/ssdi/"),
        "Casas Bahia": ('/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555',
                        "https://www.casasbahia.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t/p/1555555555"),
        "Carrefour": ('/ssd-externo-samsung-t7-1tb-mu-pc1t0t-mp9999999/p',
                      "https://www.carrefour.com.br/ssd-externo-samsung-t7-1tb-mu-pc1t0t-mp9999999/p"),
        "Americanas": ('/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p',
                       "https://www.americanas.com.br/ssd-externo-samsung-t7-1tb-usb-3-2-7485262558/p"),
    }

    def test_cada_loja_encontra_e_filtra(self):
        for loja, (href, esperado) in self.CASOS.items():
            with self.subTest(loja=loja):
                distrator = href.replace("t7-1tb", "t7-shield-1tb").replace("T7-1TB", "T7-Shield-1TB")
                distrator = distrator.replace("343491", "111").replace("B0874XN4D8", "B0SHIELD00")
                pagina = pagina_busca(distrator, href)
                self.assertEqual(monitor.extrair_candidatos(pagina, LOJAS[loja], T7), [esperado])

    def test_kingston_store(self):
        pag = pagina_busca("/products/sxs1000-1000g", "/products/sxs2000-1000g")
        self.assertEqual(monitor.extrair_candidatos(pag, LOJAS["Kingston Store"], PRODUTOS["Kingston XS1000"]),
                         ["https://www.kingstonstore.com.br/products/sxs1000-1000g"])

    def test_links_dentro_de_json_escapado(self):
        pagina = '<script>{"url":"\\u002Fproduto\\u002F343491\\u002Fssd-externo-samsung-t7-1tb"}</script>'
        self.assertEqual(monitor.extrair_candidatos(pagina, LOJAS["KaBuM!"], T7),
                         ["https://www.kabum.com.br/produto/343491/ssd-externo-samsung-t7-1tb"])

    def test_link_patrocinado_codificado_amazon(self):
        pagina = '<a href="/sspa/click?url=%2FSamsung-T7-1TB%2Fdp%2FB0874XN4D8%2Fref%3Dsr">x</a>'
        self.assertEqual(monitor.extrair_candidatos(pagina, LOJAS["Amazon"], T7),
                         ["https://www.amazon.com.br/dp/B0874XN4D8"])

    def test_sem_repetidos_e_limite(self):
        hrefs = [f"/produto/{i}/ssd-samsung-t7-1tb" for i in range(6)] + ["/produto/0/ssd-samsung-t7-1tb"]
        c = monitor.extrair_candidatos(pagina_busca(*hrefs), LOJAS["KaBuM!"], T7, limite=3)
        self.assertEqual(len(c), 3)
        self.assertEqual(len(set(c)), 3)


# ------------------------------------------------------------------ descoberta completa
def cfg_descoberta(**desc):
    return {"descoberta": {"ativa": True, "modelos_por_execucao": 2, "validade_horas": 24,
                           "candidatos_por_loja": 3, "links_por_loja": 1, **desc},
            "lojas_busca": [LOJAS["KaBuM!"], LOJAS["Kingston Store"]],
            "produtos": [T7, PRODUTOS["Kingston XS1000"]]}


class TestDescoberta(unittest.TestCase):
    AGORA = "2026-11-05 10:17"

    def respostas_basicas(self):
        busca_t7 = monitor.montar_url_busca(LOJAS["KaBuM!"]["busca"], T7["busca"])
        return {
            busca_t7: pagina_busca("/produto/1/ssd-samsung-t7-1tb-errado", "/produto/2/ssd-samsung-t7-1tb-certo"),
            "https://www.kabum.com.br/produto/1/ssd-samsung-t7-1tb-errado":
                pagina_produto("Capa protetora para SSD", 99999),       # fora da faixa e sem código
            "https://www.kabum.com.br/produto/2/ssd-samsung-t7-1tb-certo":
                pagina_produto("SSD Samsung T7 1TB MU-PC1T0T/AM", 899),
        }

    def test_descobre_valida_e_guarda(self):
        c = ColetorFalso(self.respostas_basicas(), usar_leitor=False)
        estado = monitor.descobrir(cfg_descoberta(), {}, c, self.AGORA)
        reg = estado["descobertos"]["Samsung T7"]
        self.assertEqual(reg["atualizado"], self.AGORA)
        self.assertEqual(reg["ofertas"], [{"loja": "KaBuM!",
                                           "url": "https://www.kabum.com.br/produto/2/ssd-samsung-t7-1tb-certo"}])

    def test_rejeita_produto_errado_mesmo_com_preco_valido(self):
        r = self.respostas_basicas()
        r["https://www.kabum.com.br/produto/2/ssd-samsung-t7-1tb-certo"] = pagina_produto("SSD Samsung T9 1TB", 1200)
        estado = monitor.descobrir(cfg_descoberta(), {}, ColetorFalso(r, usar_leitor=False), self.AGORA)
        self.assertEqual(estado["descobertos"]["Samsung T7"]["ofertas"], [])

    def test_loja_exclusiva_da_marca(self):
        c = ColetorFalso({}, usar_leitor=False)
        monitor.descobrir(cfg_descoberta(), {}, c, self.AGORA)
        buscas_kingston = [u for u in c.chamadas_direto if "kingstonstore" in u]
        self.assertEqual(len(buscas_kingston), 1)   # só para o Kingston XS1000, não para o T7

    def test_mantem_links_antigos_se_a_busca_falhar(self):
        antigo = {"descobertos": {"Samsung T7": {"atualizado": "2026-11-01 10:00",
                                                 "ofertas": [{"loja": "KaBuM!", "url": "https://k/1"}]}}}
        estado = monitor.descobrir(cfg_descoberta(), antigo, ColetorFalso({}, usar_leitor=False), self.AGORA)
        self.assertEqual(estado["descobertos"]["Samsung T7"]["ofertas"][0]["url"], "https://k/1")
        self.assertEqual(estado["descobertos"]["Samsung T7"]["atualizado"], self.AGORA)

    def test_respeita_validade_e_ordem(self):
        cfg = cfg_descoberta(modelos_por_execucao=1)
        estado = {"descobertos": {"Samsung T7": {"atualizado": "2026-11-05 09:00", "ofertas": []}}}
        self.assertEqual([p["nome"] for p in monitor.modelos_a_descobrir(cfg, estado, self.AGORA)],
                         ["Kingston XS1000"])
        estado["descobertos"]["Kingston XS1000"] = {"atualizado": "2026-11-05 08:00", "ofertas": []}
        self.assertEqual(monitor.modelos_a_descobrir(cfg, estado, self.AGORA), [])
        self.assertEqual(len(monitor.modelos_a_descobrir(cfg, estado, "2026-11-06 09:30")), 1)

    def test_descoberta_desligada(self):
        self.assertEqual(monitor.modelos_a_descobrir(cfg_descoberta(ativa=False), {}, self.AGORA), [])

    def test_ofertas_fixas_e_descobertas_sem_repetir(self):
        prod = {"nome": "X", "ofertas": [{"loja": "A", "url": "https://a/1"}, {"loja": "B", "url": ""},
                                         {"loja": "C", "url": "https://c/1", "ativo": False}]}
        estado = {"descobertos": {"X": {"ofertas": [{"loja": "A", "url": "https://a/1"},
                                                    {"loja": "D", "url": "https://d/1"}]}}}
        ofertas = monitor.ofertas_do_produto(prod, estado)
        self.assertEqual([(o["loja"], o["fixa"]) for o in ofertas], [("A", True), ("D", False)])

    def test_preco_da_descoberta_reaproveitado_na_consulta(self):
        c = ColetorFalso(self.respostas_basicas(), usar_leitor=False)
        cfg = cfg_descoberta()
        for p in cfg["produtos"]:
            p.setdefault("faixa_valida", [300, 4000])
        estado = monitor.descobrir(cfg, {}, c, self.AGORA)
        antes = len(c.chamadas_direto)
        novas, *_ = monitor.executar(cfg, [], c, self.AGORA, estado)
        url = "https://www.kabum.com.br/produto/2/ssd-samsung-t7-1tb-certo"
        self.assertEqual(c.chamadas_direto.count(url), 1)          # baixada uma vez só
        self.assertIn(url, [n["url"] for n in novas])

    def test_mensagem_mostra_uma_linha_por_loja(self):
        cfg = {"titulo": "T", "produtos": [{"nome": "M", "faixa_valida": [300, 4000], "ofertas": [
            {"loja": "Amazon", "url": "https://a/1"}, {"loja": "Amazon", "url": "https://a/2"},
            {"loja": "KaBuM!", "url": "https://k/1"}]}]}
        c = ColetorFalso({"https://a/1": 1000, "https://a/2": 950, "https://k/1": 990})
        _, blocos, _, _ = monitor.executar(cfg, [], c, self.AGORA)
        txt = monitor.texto_visivel("\n".join(blocos))
        self.assertIn("R$ 950 · Amazon", txt)
        self.assertEqual(txt.count("Amazon"), 1)


class TestBloqueioNaoConfundido(unittest.TestCase):
    def test_pagina_normal_sem_dado_nao_conta_como_bloqueio(self):
        c = ColetorFalso({f"https://k.com/{i}": pagina_busca("/nada") for i in range(5)}, usar_leitor=False)
        for i in range(4):
            c.coletar(f"https://k.com/{i}", [300, 4000])
        self.assertNotIn("pulado", c.coletar("https://k.com/4", [300, 4000])[2])


# ------------------------------------------------------------------ intervalo entre execuções
class TestIntervalo(unittest.TestCase):
    def rodar(self, ultima, agora, forcar=False, minimo=40):
        with tempfile.TemporaryDirectory() as d:
            e, c = Path(d, "estado.json"), Path(d, "config.json")
            c.write_text(json.dumps({"intervalo_minimo_min": minimo}))
            if ultima:
                e.write_text(json.dumps({"ultima_execucao": ultima}))
            return intervalo.deve_rodar(datetime.strptime(agora, "%Y-%m-%d %H:%M"), e, c, forcar)[0]

    def test_regras(self):
        self.assertTrue(self.rodar(None, "2026-11-05 10:00"))
        self.assertFalse(self.rodar("2026-11-05 10:00", "2026-11-05 10:25"))
        self.assertTrue(self.rodar("2026-11-05 10:00", "2026-11-05 10:45"))
        self.assertTrue(self.rodar("2026-11-05 10:00", "2026-11-05 10:05", forcar=True))


# ------------------------------------------------------------------ config real
class TestConfigCompleta(unittest.TestCase):
    def test_produtos(self):
        import re
        self.assertGreaterEqual(len(CFG["produtos"]), 10)
        nomes = [p["nome"] for p in CFG["produtos"]]
        self.assertEqual(len(nomes), len(set(nomes)))
        for p in CFG["produtos"]:
            with self.subTest(p["nome"]):
                self.assertTrue(p["busca"] and p["codigos"] and p["obrigatorias"] and p["marca"])
                for termo in p["proibidas"]:   # nenhum termo proibido pode aparecer em "/produto/"
                    self.assertNotIn(monitor.compacto(termo), "produtoportatilexternousb")

    def test_lojas(self):
        import re
        for l in CFG["lojas_busca"]:
            with self.subTest(l["loja"]):
                re.compile(l["padrao"])
                self.assertTrue(l["busca"].startswith("https://") and ("{mais}" in l["busca"] or "{hifen}" in l["busca"]))
                self.assertTrue(l["base"].startswith("https://"))

    def test_codigos_nao_se_confundem(self):
        cods = {p["nome"]: [monitor.compacto(c) for c in p["codigos"]] for p in CFG["produtos"]}
        for a, ca in cods.items():
            for b, cb in cods.items():
                if a != b:
                    for x in ca:
                        for y in cb:
                            self.assertNotIn(x, y, f"código de {a} contido no de {b}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
