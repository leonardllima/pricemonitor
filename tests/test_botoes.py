"""Testes dos botões do Telegram (offline)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
import monitor  # noqa: E402
from test_monitor import ColetorFalso, cfg_teste  # noqa: E402

monitor.ESPERA = 0
AGORA = "2026-11-18 10:17"


def rodar(respostas, historico=None, cfg=None):
    extras = {}
    novas, blocos, alerta, mudou = monitor.executar(cfg or cfg_teste(), historico or [],
                                                    ColetorFalso(respostas), AGORA, {}, extras)
    return novas, extras["botoes"]


class TestBotoes(unittest.TestCase):
    R = {"https://loja1.com/a": 1000, "https://loja2.com/a": 1100, "https://loja1.com/b": 950}

    def test_um_botao_por_modelo_na_ordem_do_ranking(self):
        _, botoes = rodar(self.R)
        self.assertEqual(len(botoes), 2)
        self.assertEqual(botoes[0][0]["text"], "🥇 Modelo B · R$ 950 · Loja1")
        self.assertEqual(botoes[0][0]["url"], "https://loja1.com/b")
        self.assertEqual(botoes[1][0]["text"], "🥈 Modelo A · R$ 1.000 · Loja1")

    def test_alerta_ganha_botao_no_topo(self):
        novas, _ = rodar(self.R)
        _, botoes = rodar(dict(self.R, **{"https://loja2.com/a": 850}), [dict(n) for n in novas])
        self.assertTrue(botoes[0][0]["text"].startswith("🎯"))
        self.assertIn("R$ 850", botoes[0][0]["text"])
        self.assertEqual(botoes[0][0]["url"], "https://loja2.com/a")

    def test_esgotado_e_falha_nao_viram_botao(self):
        _, botoes = rodar({"https://loja1.com/a": (500,), "https://loja1.com/b": 950})
        self.assertEqual([b[0]["url"] for b in botoes], ["https://loja1.com/b"])

    def test_alternativas_na_mesma_linha(self):
        cfg = cfg_teste()
        cfg["botoes"] = {"ativo": True, "alternativas_por_modelo": 1}
        _, botoes = rodar(self.R, cfg=cfg)
        linha_a = botoes[1]
        self.assertEqual(len(linha_a), 2)
        self.assertEqual(linha_a[1]["text"], "Loja2 1.100")

    def test_botoes_desligados(self):
        cfg = cfg_teste()
        cfg["botoes"] = {"ativo": False}
        self.assertEqual(rodar(self.R, cfg=cfg)[1], [])

    def test_texto_longo_e_cortado(self):
        t = monitor.texto_botao("🥇", "Nome de produto extremamente comprido demais", 1234.5,
                                "Loja Com Nome Muito Grande Também")
        self.assertLessEqual(len(t), monitor.LIMITE_TEXTO_BOTAO)
        self.assertIn("R$ 1.234", t)

    def test_limite_de_botoes(self):
        res = [{"nome": f"M{i}", "ofertas": [{"loja": "L", "url": f"https://x/{i}", "preco": 500 + i,
                                               "disp": True, "ultimo": None}]} for i in range(150)]
        self.assertEqual(len(monitor.montar_botoes(res, [])), monitor.MAX_BOTOES)

    def test_envio_com_reply_markup(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
             mock.patch.object(monitor.requests, "post") as post:
            post.return_value.status_code = 200
            monitor.telegram("oi", botoes=[[{"text": "🥇 X · R$ 900 · Loja", "url": "https://x"}]])
            markup = json.loads(post.call_args.kwargs["data"]["reply_markup"])
            self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://x")

    def test_botoes_so_na_ultima_mensagem(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, hist = Path(d, "config.json"), Path(d, "historico.csv")
            cfg_path.write_text(json.dumps(cfg_teste()), encoding="utf-8")
            with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                 mock.patch.object(monitor, "dividir_mensagem", lambda b: ["parte 1", "parte 2"]), \
                 mock.patch.object(monitor.requests, "post") as post:
                post.return_value.status_code = 200
                monitor.main(cfg_path, hist, ColetorFalso(self.R))
                chamadas = [c.kwargs["data"] for c in post.call_args_list]
                self.assertNotIn("reply_markup", chamadas[0])
                self.assertIn("reply_markup", chamadas[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
