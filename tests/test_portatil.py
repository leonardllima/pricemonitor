"""Garante que só SSDs PORTÁTEIS entram no monitor, e testa a mecânica de hora em hora."""
import csv
import json
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import intervalo  # noqa: E402
import monitor  # noqa: E402
from test_descoberta import pagina_busca, pagina_produto  # noqa: E402
from test_monitor import ColetorFalso  # noqa: E402

monitor.ESPERA = 0
CFG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
P = {p["nome"]: p for p in CFG["produtos"]}
LOJAS = {l["loja"]: l for l in CFG["lojas_busca"]}

# Títulos REAIS de produtos que NÃO podem entrar, por modelo que poderia confundir
INTERNOS = {
    "SanDisk Extreme": ["SSD SanDisk Extreme de 1 TB M.2 NVMe PCIe Gen 4.0",
                        "SSD SanDisk Extreme 1TB M2 2280 NVMe"],
    "SanDisk Extreme PRO": ["SSD SanDisk Extreme PRO 1TB M.2 2280 PCIe Gen4 NVMe com dissipador"],
    "SanDisk Portable": ["SSD SanDisk Plus 1TB SATA III 2,5 - SDSSDA-1T00-G27",
                         "SSD SanDisk Ultra 3D 1TB SATA 2,5 polegadas",
                         "SSD Interno SanDisk 1TB SATA"],
    "Samsung T7": ["SSD Samsung 990 EVO 1TB M.2 2280 NVMe (MZ-V9E1T0)"],
    "Kingston XS1000": ["SSD Kingston NV3 1TB M.2 2280 NVMe PCIe 4.0 SNV3S/1000G"],
    "Crucial X9": ["SSD Crucial P3 Plus 1TB M.2 NVMe CT1000P3PSSD8"],
    "WD My Passport SSD": ["SSD WD Blue SN580 1TB M.2 2280 NVMe interno"],
}
ACESSORIOS = {
    "Kingston XS1000": ["Estojo Rígido para SSD Kingston XS1000 SXS1000/1000G"],
    "Samsung T7": ["Capa protetora para SSD Samsung T7 1TB MU-PC1T0T", "Case Para Samsung T7 Portable SSD 1TB"],
    "SanDisk Extreme": ["Capa de silicone para SanDisk Extreme Portable SSD 1TB"],
    "Crucial X9": ["Gaveta USB-C para SSD Crucial X9 1TB"],
}
# Títulos REAIS de produtos portáteis que DEVEM entrar
PORTATEIS = {
    "Kingston XS1000": "SSD Externo Portátil Kingston XS1000 1TB, USB 3.2, Leitura 1050MB/S, SXS1000/1000G",
    "Kingston XS2000": "Ssd.Externo 1tb USB C 3.2 2000mb/s Leit C/ Capa de borracha SXS2000/1000G Kingston",
    "Samsung T7": "SSD Externo Samsung T7 1TB (USB 3.2, Leitura até 1050MB/s e Gravação até 1000MB/s)",
    "Samsung T7 Shield": "SAMSUNG T7 Shield 1 TB, até 1050 MB/s, USB 3.2 Gen2, resistente, IP65",
    "Samsung T9": "SSD Portátil Samsung T9 1TB USB 3.2 Gen 2x2 MU-PG1T0B/AM",
    "SanDisk Extreme": "SSD Externo Portátil SanDisk Extreme, 1TB, USB 3.1 Type C - SDSSDE61-1T00-G25",
    "SanDisk Extreme PRO": "SSD Portátil SanDisk Extreme PRO 1TB USB-C 2000MB/s SDSSDE81-1T00-G25",
    "SanDisk Portable": "SSD Portátil SanDisk 1TB USB-C 800MB/s SDSSDE30-1T00-G26",
    "Crucial X9": "Crucial SSD portátil X9 de 1 TB, até 1050 MB/s, USB 3.2 USB-C",
    "Crucial X9 Pro": "SSD Externo Crucial X9 Pro 1TB USB-C CT1000X9PROSSD9",
    "Crucial X10 Pro": "SSD Externo Crucial X10 Pro 1TB USB-C 2100MB/s CT1000X10PROSSD9",
    "WD My Passport SSD": "SSD Externo WD My Passport 1TB USB-C WDBAGF0010BGY",
}


def slug(titulo):
    t = monitor.normal(titulo)
    return "/produto/123/" + re.sub(r"[^a-z0-9]+", "-", t).strip("-")


class TestSoPortateis(unittest.TestCase):
    def test_paginas_de_ssd_interno_sao_rejeitadas(self):
        for nome, titulos in INTERNOS.items():
            for t in titulos:
                with self.subTest(modelo=nome, titulo=t):
                    self.assertFalse(monitor.produto_confere(pagina_produto(t, 600), P[nome]))

    def test_links_de_ssd_interno_sao_rejeitados_na_busca(self):
        for nome, titulos in INTERNOS.items():
            for t in titulos:
                with self.subTest(modelo=nome, titulo=t):
                    self.assertFalse(monitor.candidato_ok(slug(t), P[nome]))

    def test_acessorios_sao_rejeitados(self):
        for nome, titulos in ACESSORIOS.items():
            for t in titulos:
                with self.subTest(modelo=nome, titulo=t):
                    self.assertFalse(monitor.produto_confere(pagina_produto(t, 600), P[nome]))
                    self.assertFalse(monitor.candidato_ok(slug(t), P[nome]))

    def test_todos_os_portateis_sao_aceitos(self):
        for nome, t in PORTATEIS.items():
            with self.subTest(modelo=nome):
                self.assertTrue(monitor.produto_confere(pagina_produto(t, 1000), P[nome]))
                self.assertTrue(monitor.candidato_ok(slug(t), P[nome]))

    def test_cada_portatil_so_casa_com_o_proprio_modelo(self):
        for nome_titulo, t in PORTATEIS.items():
            for nome_modelo, prod in P.items():
                if nome_modelo != nome_titulo:
                    with self.subTest(titulo=nome_titulo, modelo=nome_modelo):
                        self.assertFalse(monitor.produto_confere(pagina_produto(t, 1000), prod))

    def test_sem_codigo_exige_palavra_de_portatil(self):
        pagina = pagina_produto("SanDisk 1TB", 900)   # sem código, sem USB/externo/portátil
        self.assertFalse(monitor.produto_confere(pagina, P["SanDisk Portable"]))

    def test_amazon_slug_com_acento_codificado(self):
        href = "/Crucial-SSD-port%C3%A1til-X9-CT1000X9SSD902/dp/B0CGW1FQV4"
        self.assertEqual(monitor.extrair_candidatos(pagina_busca(href), LOJAS["Amazon"], P["Crucial X9"]),
                         ["https://www.amazon.com.br/dp/B0CGW1FQV4"])

    def test_busca_com_internos_e_portatil_misturados(self):
        hrefs = [slug(t) for t in INTERNOS["SanDisk Extreme"]] + [slug(PORTATEIS["SanDisk Extreme"])]
        c = monitor.extrair_candidatos(pagina_busca(*hrefs), LOJAS["KaBuM!"], P["SanDisk Extreme"])
        self.assertEqual(len(c), 1)
        self.assertIn("portatil", c[0])


class TestLimpezaPorVersao(unittest.TestCase):
    def preparar(self, d):
        cfg = {"descoberta": {"versao": 2}, "produtos": [
            {"nome": "M", "ofertas": [{"loja": "Fixa", "url": "https://fixa/1"}]}]}
        estado = {"ultima_execucao": "2026-11-19 09:07", "descobertos": {"M": {"atualizado": "x", "ofertas": [
            {"loja": "A", "url": "https://interno/1"}, {"loja": "B", "url": "https://fixa/1"}]}}}
        hist = [{"data_hora": "t", "produto": "M", "loja": "A", "preco": "500", "disponivel": "1", "url": "https://interno/1"},
                {"data_hora": "t", "produto": "M", "loja": "Fixa", "preco": "900", "disponivel": "1", "url": "https://fixa/1"},
                {"data_hora": "t", "produto": "M", "loja": "Outra", "preco": "950", "disponivel": "1", "url": "https://outra/1"}]
        return cfg, estado, hist

    def test_troca_de_versao_limpa_links_e_historico(self):
        cfg, estado, hist = self.preparar(None)
        limpo, removidas, trocou = monitor.aplicar_versao_regras(cfg, estado, hist)
        self.assertTrue(trocou)
        self.assertEqual(removidas, 1)
        self.assertEqual([h["url"] for h in limpo], ["https://fixa/1", "https://outra/1"])
        self.assertEqual(estado["descobertos"], {})
        self.assertEqual(estado["versao_descoberta"], 2)
        self.assertEqual(estado["ultima_execucao"], "2026-11-19 09:07")   # intervalo continua valendo

    def test_mesma_versao_nao_mexe(self):
        cfg, estado, hist = self.preparar(None)
        estado["versao_descoberta"] = 2
        limpo, removidas, trocou = monitor.aplicar_versao_regras(cfg, estado, hist)
        self.assertEqual((len(limpo), removidas, trocou), (3, 0, False))
        self.assertEqual(len(estado["descobertos"]["M"]["ofertas"]), 2)

    def test_reescrever_historico(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = Path(d, "historico.csv")
            _, _, hist = self.preparar(None)
            monitor.gravar_historico(hist, caminho)
            monitor.reescrever_historico(hist[1:], caminho)
            with caminho.open(encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)

    def test_config_real_usa_versao_nova(self):
        self.assertGreaterEqual(CFG["descoberta"]["versao"], 2)


class TestDisparoDeHoraEmHora(unittest.TestCase):
    def crons(self):
        yml = (RAIZ / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
        return re.findall(r'cron:\s*"([^"]+)"', yml)

    def test_workflow_tem_3_disparos_por_hora(self):
        minutos = sorted(int(c.split()[0]) for c in self.crons())
        self.assertEqual(len(minutos), 3)
        for c in self.crons():
            self.assertEqual(c.split()[1:], ["*", "*", "*", "*"])
        self.assertNotIn(0, minutos)
        intervalos = [(minutos[(i + 1) % 3] - minutos[i]) % 60 for i in range(3)]
        self.assertTrue(all(i >= 15 for i in intervalos))   # bem espalhados na hora

    def test_intervalo_minimo_combina_com_o_cron(self):
        minimo = CFG["intervalo_minimo_min"]
        minutos = sorted(int(c.split()[0]) for c in self.crons())
        maior_salto = max((minutos[(i + 1) % 3] - minutos[i]) % 60 for i in range(3))
        self.assertGreater(minimo, 60 - maior_salto - 1)   # o 2º e 3º disparo da hora são pulados
        self.assertLess(minimo, 60)                        # o disparo da hora seguinte sempre roda

    def simular_dia(self, prob_perda, atraso_max, seed):
        """Simula 24 h de disparos do GitHub (com perdas e atrasos) passando pelo intervalo.py."""
        rnd = random.Random(seed)
        minutos = [int(c.split()[0]) for c in self.crons()]
        inicio = datetime(2026, 11, 19, 0, 0)
        disparos = sorted(inicio + timedelta(hours=h, minutes=m + rnd.randint(0, atraso_max))
                          for h in range(24) for m in minutos if rnd.random() > prob_perda)
        execucoes = []
        with tempfile.TemporaryDirectory() as d:
            e, c = Path(d, "estado.json"), Path(d, "config.json")
            c.write_text(json.dumps({"intervalo_minimo_min": CFG["intervalo_minimo_min"]}))
            for t in disparos:
                rodar, _ = intervalo.deve_rodar(t, e, c)
                if rodar:
                    execucoes.append(t)
                    e.write_text(json.dumps({"ultima_execucao": t.strftime("%Y-%m-%d %H:%M")}))
        return execucoes

    def test_sem_falhas_roda_exatamente_uma_vez_por_hora(self):
        ex = self.simular_dia(prob_perda=0, atraso_max=0, seed=1)
        self.assertEqual(len(ex), 24)
        self.assertTrue(all((b - a) == timedelta(hours=1) for a, b in zip(ex, ex[1:])))

    def test_com_perdas_e_atrasos_nunca_duplica(self):
        # cenário pessimista: GitHub perde 30% dos disparos e atrasa até 15 min
        for seed in range(30):
            with self.subTest(seed=seed):
                ex = self.simular_dia(prob_perda=0.3, atraso_max=15, seed=seed)
                saltos = [(b - a).total_seconds() / 60 for a, b in zip(ex, ex[1:])]
                self.assertTrue(all(s >= CFG["intervalo_minimo_min"] for s in saltos))   # nunca duplica
                self.assertGreaterEqual(len(ex), 18)
                self.assertTrue(all(s < 180 for s in saltos))   # mesmo no pior caso, nunca 3 h sem consulta

    def test_cenario_tipico_quase_todas_as_horas(self):
        # cenário típico: 10% de perda e até 10 min de atraso
        total = [len(self.simular_dia(prob_perda=0.1, atraso_max=10, seed=s)) for s in range(30)]
        self.assertGreaterEqual(min(total), 22)
        self.assertGreaterEqual(sum(total) / len(total), 23)


if __name__ == "__main__":
    unittest.main(verbosity=2)
