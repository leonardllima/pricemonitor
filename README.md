# Monitor de preços — Kingston XS1000 1TB

Roda grátis no GitHub Actions a cada 6 horas, salva o histórico em `historico.csv`
e avisa no Telegram quando o preço atinge o alvo, cai X% ou bate o menor preço já visto.

## 1. Criar o bot do Telegram (2 min)
1. No Telegram, fale com **@BotFather** → `/newbot` → escolha um nome. Ele te dá o **token**.
2. Mande qualquer mensagem para o seu bot novo.
3. Abra no navegador `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
   e copie o número em `"chat":{"id": ...}` — esse é o **chat id**.

## 2. Subir no GitHub
1. Crie um repositório (pode ser **privado**) e envie estes arquivos (inclusive a pasta `.github`).
2. Em **Settings → Secrets and variables → Actions → New repository secret**, crie:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`

## 3. Configurar os produtos (`config.json`)
- Cole o link da página do produto em cada loja (troque os `COLE_AQUI...`).
- `preco_alvo`: valor em que você quer ser avisado.
- `faixa_valida`: descarta números absurdos (ex.: valor de parcela).
- `queda_minima_pct`: % de queda para disparar alerta.
- Pode adicionar quantas lojas e produtos quiser.
- Se uma loja não for reconhecida, adicione `"seletor": "css-do-preço"` na oferta.

## 4. Testar
**Actions → Monitor de preços → Run workflow** (com "resumo" marcado).
Você recebe o resumo no Telegram e o log mostra ✓/✗ por loja.

## Rodar no seu PC (opcional)
```
pip install -r requirements.txt
python monitor.py
```
Sem as variáveis do Telegram, as mensagens aparecem no terminal.
