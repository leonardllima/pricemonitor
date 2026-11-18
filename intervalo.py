#!/usr/bin/env python3
"""Decide se a coleta deve rodar agora (evita execuções duplicadas quando o
agendamento do GitHub e o cron-job.org disparam perto um do outro).
Usa só a biblioteca padrão: roda antes de instalar as dependências."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
FUSO_BR = timezone(timedelta(hours=-3))


def deve_rodar(agora=None, estado_path=BASE / "estado.json", config_path=BASE / "config.json", forcar=False):
    if forcar:
        return True, "execução forçada"
    try:
        minimo = json.loads(Path(config_path).read_text(encoding="utf-8")).get("intervalo_minimo_min", 40)
        ultima = json.loads(Path(estado_path).read_text(encoding="utf-8"))["ultima_execucao"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return True, "sem registro da última execução"
    agora = agora or datetime.now(FUSO_BR).replace(tzinfo=None)
    passado = agora - datetime.strptime(ultima, "%Y-%m-%d %H:%M")
    if passado < timedelta(minutes=minimo):
        return False, f"última consulta há {int(passado.total_seconds() // 60)} min (mínimo {minimo})"
    return True, f"última consulta há {int(passado.total_seconds() // 60)} min"


if __name__ == "__main__":
    rodar, motivo = deve_rodar(forcar=os.getenv("FORCAR", "").lower() == "true")
    print(("▶️ Rodando: " if rodar else "⏭️ Pulando: ") + motivo)
    saida = os.getenv("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a") as f:
            f.write(f"rodar={'true' if rodar else 'false'}\n")
