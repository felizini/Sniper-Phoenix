# 🐦‍🔥 Sniper Phoenix v5.2.0 — Documentação Técnica Completa

> **Bot de trading automatizado para a Binance com DCA inteligente, detecção de anomalias e sistema híbrido de decisão em tempo real.**

---

## Índice

1. [Visão Geral](#1-visão-geral)
2. [Arquitetura e Threads](#2-arquitetura-e-threads)
3. [Estratégia de Trading](#3-estratégia-de-trading)
4. [Filtros de Entrada (M1–M3)](#4-filtros-de-entrada-m1m3)
5. [Gestão de Risco (M4–M5)](#5-gestão-de-risco-m4m5)
6. [Detector de Anomalias (M6)](#6-detector-de-anomalias-m6)
7. [Sistema Híbrido (v5.2)](#7-sistema-híbrido-v52)
8. [Vault e Execução de Ordens](#8-vault-e-execução-de-ordens)
9. [Persistência e Auditoria](#9-persistência-e-auditoria)
10. [Dashboard e Interface](#10-dashboard-e-interface)
11. [Configuração (config.ini)](#11-configuração-configini)
12. [Histórico de Versões](#12-histórico-de-versões)

---

## 1. Visão Geral

O Sniper Phoenix é um bot de trading automatizado desenvolvido em Python que opera na Binance via API. Sua estratégia central é o **DCA (Dollar Cost Averaging)** com múltiplas camadas de proteção e captura de oportunidades.

### Características principais

| Característica | Detalhe |
|---|---|
| Exchange | Binance (via CCXT) |
| Par padrão | PAXG/USDT |
| Estratégia base | DCA com Safety Orders |
| Saída | Trailing Stop adaptativo (ATR) |
| Proteção | Stop Loss em USD + Circuit Breaker |
| Detecção | Flash Pumps e Flash Crashes em tempo real |
| Latência (detecção) | 10–15 segundos (WebSocket tick-by-tick) |
| Arquivos de log | `sniper_system.log`, `sniper_trades.csv` |
| Persistência | `sniper_state.json` (recuperação após reinício) |

### Evolução de versões resumida

```
v4.3.5  →  v5.0.3  →  v5.1.0  →  v5.2.0
(base)     (backtest) (M6: detector velas) (híbrido RT+1h)
```

---

## 2. Arquitetura e Threads

O bot opera com **4 threads paralelas** mais um loop asyncio para WebSocket:

```
┌─────────────────────────────────────────────────────────────┐
│                    SNIPER PHOENIX v5.2.0                    │
├──────────────┬──────────────┬──────────────┬───────────────-┤
│ thread_motor │thread_scanner│thread_visual │ WebSocket-     │
│              │              │              │ Ticker         │
│ Loop DCA     │ MTF (EMA)    │ Dashboard    │ @ticker        │
│ Stop Loss    │ Circuit Bkr  │ Console      │ shared_state   │
│ Trailing     │ RSI atualiz. │ 1s refresh   │ ["preco"]      │
│ Anomalias    │ 30s interval │              │                │
├──────────────┴──────────────┴──────────────┴────────────────┤
│                   WebSocket-Trade [NOVO v5.2]               │
│   @trade stream → tick-by-tick → RealtimeAnomalyDetector    │
└─────────────────────────────────────────────────────────────┘
                          │
                    state_lock
                   (threading.Lock)
                          │
                    shared_state (dict)
```

### Comunicação entre threads

Todas as threads leem e escrevem em `shared_state` protegido por `state_lock` (threading.Lock). Nenhuma thread chama a API da Binance fora de sua responsabilidade designada.

| Thread | Lê | Escreve |
|---|---|---|
| Motor | `mtf`, `preco`, `circuit_breaker` | `em_operacao`, `marcha`, `lucro_perc_atual` |
| Scanner | — | `mtf`, `filtros`, `rsi_atual`, `circuit_breaker` |
| Visual | tudo (leitura) | — |
| WebSocket Ticker | — | `preco` |
| WebSocket Trade | — | alimenta `realtime_detector` (direto, thread-safe) |

---

## 3. Estratégia de Trading

### 3.1 Ciclo completo

```
AGUARDANDO SINAL
      │
      ▼ (MTF ≥ 2/3 timeframes + filtros M1+M2 ok + CB inativo)
COMPRA BASE (capital_base em USDT)
      │
      ├──► Preço cai → DCA Safety Order (até MAX_SAFETY_ORDERS)
      │         └─ Cada SO: volume × dca_volume_scale
      │                     step × dca_step_scale
      │
      ├──► Preço sobe ≥ trailing_trigger → ATIVA TRAILING STOP
      │         └─ Stop = max_price × (1 - ATR_dist)
      │                   └─ Fecha quando preço ≤ stop
      │
      ├──► Flash Pump detectado → VENDA DE EMERGÊNCIA (híbrido)
      │
      ├──► Flash Crash detectado → COMPRA DE EMERGÊNCIA
      │
      └──► Perda ≥ stop_loss_usd → STOP LOSS em USD
```

### 3.2 DCA — Safety Orders

A fórmula das Safety Orders escala tanto o volume quanto o step de entrada a cada nível:

```
SO #1: vol = capital_base × dca_volume_scale¹   step = dca_step_initial × dca_step_scale⁰
SO #2: vol = capital_base × dca_volume_scale²   step = dca_step_initial × dca_step_scale¹
SO #3: vol = capital_base × dca_volume_scale³   step = dca_step_initial × dca_step_scale²
```

**Exemplo com configuração padrão** (`capital_base=14.75`, `scale=1.5`, `step_init=2%`, `step_scale=1.3`):

| SO | Volume USD | Step de queda |
|---|---|---|
| Entrada | $14.75 | — |
| #1 | $22.13 | 2.0% |
| #2 | $33.19 | 2.6% |
| #3 | $49.78 | 3.4% |

O DCA fica **bloqueado** quando uma venda parcial híbrida está ativa (aguardando confirmação).

### 3.3 Preço médio ponderado

A cada nova Safety Order executada, o preço médio é recalculado:

```python
pm = sum(ordem['custo'] for ordem in ordens_executadas) /
     sum(ordem['volume'] for ordem in ordens_executadas)
```

### 3.4 Trailing Stop com ATR

O trailing só ativa quando o preço supera o **gatilho**:

```
gatilho_ts = preco_medio × (1 + trailing_trigger)   # ex: +1.5%
```

Uma vez ativo, o stop segue o preço máximo da sessão:

```
stop_price = max_price × (1 - trailing_dist_ATR)
```

A distância é calculada dinamicamente pelo ATR(14) da vela 1h:

```python
trailing_dist = ATR_percentual × ATR_multiplicador   # mínimo = TRAILING_DIST (fallback)
```

---

## 4. Filtros de Entrada (M1–M3)

Antes de qualquer compra de entrada, três filtros são verificados em sequência:

### [M1] Filtro RSI

```
RSI(14) da vela 1h < RSI_MAX_ENTRADA (padrão: 65)
```

Evita entrar em ativo sobrecomprado. O RSI é recalculado a cada 30 segundos pelo scanner e exibido no dashboard em tempo real.

### [M2] Confirmação de Volume

```
volume_atual_1h ≥ media_20_velas × VOLUME_FATOR_MIN (padrão: 1.2×)
```

Exige que o volume da vela atual seja ao menos 20% acima da média dos últimos 20 períodos. Evita entrar em movimentos sem liquidez.

### [M3] Circuit Breaker

```
se variação nas últimas CB_JANELA_VELAS (5) velas de 4h ≤ -CB_QUEDA_PCT (3%)
    → BLOQUEIA todas as novas entradas
```

Pausa o bot durante quedas bruscas de mercado. A análise é feita sobre velas de 4h, cobrindo uma janela de ~20 horas. Novas entradas ficam bloqueadas até o mercado se estabilizar.

### [MTF] Análise Multi-Timeframe

O scanner verifica a tendência em 3 timeframes via EMA:

| Timeframe | EMA | Condição de "bullish" |
|---|---|---|
| 15m | EMA(25) | close > EMA |
| 1h | EMA(50) | close > EMA |
| 4h | EMA(100) | close > EMA |

**Entrada só ocorre com ≥ 2 de 3 timeframes favoráveis.**

---

## 5. Gestão de Risco (M4–M5)

### [M4] Stop Loss Dinâmico em USD

```
se perda_atual_USD ≥ STOP_LOSS_USD (padrão: $10)
    → VENDA TOTAL imediata + encerra ciclo
```

Opera sobre o **prejuízo absoluto em dólares**, não sobre percentual. Funciona a qualquer momento do ciclo, independentemente de quantas Safety Orders foram executadas. É verificado a cada iteração do loop principal.

### [M5] ATR Adaptativo

O Trailing Stop se ajusta à volatilidade real do ativo. Em momentos de alta volatilidade, o stop recua mais para não ser atingido prematuramente; em momentos calmos, aperta para capturar mais do lucro.

```
ATR_dist = ATR(14)_percentual × 1.5   # mínimo = TRAILING_DIST (fallback)
```

O ATR é recalculado a cada nova Safety Order e no início de cada ciclo.

---

## 6. Detector de Anomalias (M6)

Introduzido na v5.1.0, o `AnomalyDetector` analisa velas **1h fechadas** e detecta eventos estatisticamente anômalos.

### Critérios de detecção (todos necessários)

| Critério | Threshold padrão (PAXG) |
|---|---|
| Spike de preço | ≥ 0.5% (high vs open ou low vs open) |
| Volume × média | ≥ 5× a média dos últimos 20 períodos |
| Trades × média | ≥ 10× a média dos últimos 20 períodos |

### Cálculo de confiança

```python
price_score  = min(spike_pct / PRICE_SPIKE_THRESHOLD,  2.0) / 2.0
volume_score = min(vol_ratio / VOLUME_SPIKE_THRESHOLD,  2.0) / 2.0
trades_score = min(trd_ratio / TRADES_SPIKE_THRESHOLD,  2.0) / 2.0

confidence = (price_score * 0.4) + (volume_score * 0.35) + (trades_score * 0.25)
```

### Ações de emergência

**Flash Pump detectado (em posição):**
- Confiança ≥ 70% e lucro atual ≥ 0% → **VENDA DE EMERGÊNCIA**
- Ignora trailing stop e RSI
- Vende ao preço de mercado imediatamente

**Flash Crash detectado (sem posição):**
- Confiança ≥ 80% e capital disponível > 0 → **COMPRA DE EMERGÊNCIA**
- Compra no fundo do crash antes da recuperação

---

## 7. Sistema Híbrido (v5.2)

A grande novidade da v5.2 é o **HybridDetector**, que combina dois detectores com pesos diferentes para decisões mais rápidas e confiáveis.

### 7.1 Componentes

```
┌─────────────────────────────────────────────────────────────┐
│                    HybridDetector                           │
│                                                             │
│  RealtimeAnomalyDetector           AnomalyDetector (1h)    │
│  ─────────────────────             ────────────────────     │
│  Fonte: @trade WebSocket           Fonte: REST API (1h)     │
│  Latência: 10–15s                  Latência: até 1h         │
│  Frequência: tick-by-tick          Frequência: por vela     │
│  Peso: 0.6                         Peso: 1.0                │
│                                                             │
│  Thresholds RT (mais sensíveis):   Thresholds 1h:           │
│  Spike:  ≥ 0.3%                   Spike:  ≥ 0.5%           │
│  Volume: ≥ 3× média               Volume: ≥ 5× média        │
│  Velocidade: ≥ 0.1%/seg           Trades: ≥ 10× média      │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 Lógica de decisão para Flash Pump

```
score_RT = confiança_RT × 0.6
score_1h = confiança_1h × 1.0

┌──────────────────────────────────────────────────────────────┐
│ CENÁRIO                    │ AÇÃO                            │
├────────────────────────────┼─────────────────────────────────┤
│ RT + 1h detectam juntos    │ SELL_ALL imediato               │
│ (score_RT + score_1h ≥ th) │ Score combinado (cap 100%)     │
├────────────────────────────┼─────────────────────────────────┤
│ Só 1h detecta              │ SELL_ALL imediato               │
│ (score_1h ≥ threshold)     │ Comportamento v5.1.0           │
├────────────────────────────┼─────────────────────────────────┤
│ Só RT detecta              │ SELL_PARTIAL (50%)              │
│ (score_RT ≥ threshold×0.6) │ Aguarda confirmação 1h          │
│                            │ ou timeout de 5 minutos         │
├────────────────────────────┼─────────────────────────────────┤
│ Partial ativa + 1h confirma│ SELL_ALL_REMAINING (50% resto)  │
├────────────────────────────┼─────────────────────────────────┤
│ Partial ativa + timeout    │ SELL_ALL_REMAINING (forçado)    │
└────────────────────────────┴─────────────────────────────────┘
```

### 7.3 Fluxo de venda parcial

```
RT detecta pump (sozinho)
        │
        ▼
Vende 50% imediatamente ao preço de mercado
Registra: partial_sell_ativa = True
Bloqueia DCA durante espera
        │
        ├── Aguarda até 300s (5 min)
        │
        ├── 1h confirma anomalia?
        │       └── SIM → Vende 50% restante → Encerra ciclo
        │
        └── Timeout expirou?
                └── SIM → Vende 50% restante → Encerra ciclo
```

### 7.4 Baseline do detector RT

O `RealtimeAnomalyDetector` precisa de um período de aprendizado antes de agir:

```
RT_BASELINE_MINUTES = 60  (padrão)

Durante os primeiros 60 minutos:
→ Acumula histórico de preços, volumes e velocidades
→ NÃO emite alertas nem age
→ Dashboard exibe: "RT aguard.baseline (N/60)"

Após baseline pronto:
→ Dashboard exibe: "RT OK"
→ Detecção em tempo real ativa
```

### 7.5 Vela parcial em tempo real

O RT não espera velas fecharem — constrói a vela atual tick a tick:

```python
# A cada tick recebido via @trade WebSocket:
current_minute_open   = primeiro_tick_do_minuto
current_minute_high   = max(high, tick_price)
current_minute_low    = min(low, tick_price)
current_minute_close  = tick_price
current_minute_volume += tick_quantity
```

Quando o minuto fecha, a vela parcial vira histórico e o baseline é atualizado.

### 7.6 Detecção de velocidade

O RT também detecta anomalias pela **velocidade** de mudança de preço:

```python
velocity = abs(price_change_pct) / elapsed_seconds   # % por segundo

se velocity ≥ RT_VELOCITY_THRESHOLD (0.1%/seg)
    → contribui para o score do spike
```

Isso permite detectar flash crashes/pumps mesmo antes que o volume se acumule.

---

## 8. Vault e Execução de Ordens

### 8.1 Sistema de Vault

O capital fica protegido na carteira **Funding (Fundos)** da Binance. Só é movido para **Spot** no momento exato de uma compra:

```
FUNDING ACCOUNT (protegido)
        │
        │ transferir_para_spot(valor)
        ▼
SPOT ACCOUNT
        │
        │ create_order(market, buy)
        ▼
ATIVO COMPRADO
        │
        │ create_order(market, sell)
        ▼
USDT em SPOT
        │
        │ recolher_para_fundos()
        ▼
FUNDING ACCOUNT (protegido)
```

### 8.2 Segurança na inicialização

Ao iniciar o bot, `inicializar_vault()` recolhe qualquer USDT que estiver no Spot de volta para Funding, garantindo que não há capital exposto desnecessariamente.

### 8.3 Tratamento de erros

Compras e vendas possuem contador de erros consecutivos. Após **5 falhas seguidas**, o bot aciona `panico_sistema()` e encerra de forma segura, recolhendo o que for possível ao Vault.

---

## 9. Persistência e Auditoria

### 9.1 Estado em disco (`sniper_state.json`)

Salvo a cada operação relevante:

```json
{
  "em_operacao": true,
  "ordens": [
    {"p": 3250.50, "volume": 0.00453, "custo": 14.75}
  ],
  "symbol": "PAXG/USDT",
  "max_p_trailing": 3310.00,
  "trailing_ativo": false,
  "timestamp": "2026-03-02 14:35:22"
}
```

Na inicialização, se o arquivo existir e o símbolo coincidir, o bot **retoma o ciclo de onde parou** sem perder posição.

### 9.2 Log de sistema (`sniper_system.log`)

Todos os eventos relevantes com timestamp:

```
2026-03-02 14:35:22 - INFO - ESTADO RESTAURADO: 2 ordens.
2026-03-02 14:35:50 - WARNING - [RT] FLASH PUMP detectado! Spike: 0.84% | Vol: 12.3× | Vel: 0.003%/s | Conf: 78%
2026-03-02 14:35:51 - INFO - Hybrid PARTIAL SELL executado: $14.78
```

### 9.3 Log de trades (`sniper_trades.csv`)

Cada transação registrada em CSV para análise posterior:

```
DATA, PAR, TIPO, PRECO, QTD, TOTAL_USD, LUCRO_USD, LUCRO_PERC, SALDO_VAULT, OBS
2026-03-02 14:35:51, PAXG/USDT, PARTIAL SELL HYBRID, 3305.40, 0.00224, 7.40, 0.31, 2.13%, 142.80, Hybrid RT-only score 78%
```

### 9.4 Hot Reload de configuração

Pelo menu (Ctrl+C → opção 2), o `config.ini` pode ser recarregado em produção sem reiniciar o bot. Todos os parâmetros são aplicados na próxima iteração do loop.

---

## 10. Dashboard e Interface

O dashboard é exibido no terminal, atualizado a cada 1 segundo:

```
🐦‍🔥 SNIPER PHOENIX v5.2.0 | DETECTOR HÍBRIDO | UPTIME: 02:14:33
================================================================================
  MERCADO: PAXG/USDT $3,285.40000 | MTF [15m:▲][1h:▲][4h:▼] | RSI 52.3
   STATUS: DCA ATIVO — 2 ordens executadas
  HÍBRIDO: [RT OK | 1h: OK] | Pumps RT: 2 | Sells Hybrid: 1 | Parciais: 1
  POSIÇÃO: PM $3,251.20 | Lucro: +1.05% ($3.42) | SO: 2/3
  DCA PROX: $3,186.18 (-2.00%)
  TRAILING: ATIVO (Stop: $3,272.50 | Recuo: 0.39% | ATR dist: 1.23%)
================================================================================
 LOG: ✅ Trailing Ativado! Dist ATR: 1.23%
================================================================================
 [Ctrl+C] MENU | LOGS: sniper_system.log | TRADES: sniper_trades.csv
```

### Menu de controle (Ctrl+C)

```
╔══════════════════════════════════════╗
║    MENU DE CONTROLE SNIPER v5.2      ║
╠══════════════════════════════════════╣
║ 1. VOLTAR AO MONITORAMENTO           ║
║ 2. ATUALIZAR CONFIG.INI (HOT RELOAD) ║
║ 3. ENCERRAR (DESLIGAR BOT)           ║
╚══════════════════════════════════════╝
```

### Dashboard HTML (`sniper_phoenix_dashboard.html`)

Interface web separada para análise de histórico de trades com:
- Gráfico de equity curve por ciclo
- Tabela de todas as transações com filtros
- Estatísticas: win rate, profit factor, Sharpe, max drawdown
- Export para CSV e JSON
- Visualização de ciclos de DCA com suas ordens

### Backtest Engine (`sniper_backtest.html`)

Ferramenta de backtesting integrada:
- Testa parâmetros sobre dados históricos OHLCV
- Ranking de configurações testadas (salvo em localStorage)
- Export direto para `config.ini` da melhor configuração encontrada

---

## 11. Configuração (config.ini)

### Seção `[trading]` — Parâmetros principais

```ini
[trading]
symbol = PAXG/USDT
capital_base = 14.75          # Valor da ordem de entrada ($)
max_safety_orders = 3         # Máximo de Safety Orders

dca_volume_scale = 1.5        # Multiplicador de volume por SO
dca_step_initial = 0.02       # Queda para primeira SO (2%)
dca_step_scale = 1.3          # Escala do step entre SOs

trailing_trigger = 0.015      # Gatilho do trailing (+1.5%)
trailing_dist = 0.006         # Distância mínima fallback (0.6%)
stop_loss_usd = 10.0          # Stop loss absoluto em USD

atr_period = 14               # Período do ATR
atr_multiplicador = 1.5       # Multiplicador ATR para trailing

rsi_max_entrada = 65.0        # RSI máximo para entrada
rsi_period = 14

volume_fator_min = 1.2        # Volume mínimo vs média (1.2×)

cb_queda_pct = 3.0            # Queda para circuit breaker (3%)
cb_janela_velas = 5           # Velas de 4h para medir
```

### Seção `[anomaly]` — Detector de velas 1h

```ini
[anomaly]
enabled = true
price_spike_threshold = 0.005     # 0.5% para PAXG
volume_spike_threshold = 5.0      # 5× média
trades_spike_threshold = 10.0     # 10× média
lookback_candles = 20
emergency_sell_min_confidence = 0.70
emergency_buy_min_confidence = 0.80
```

### Seção `[hybrid]` — Detector em tempo real (v5.2)

```ini
[hybrid]
enabled = true

rt_baseline_minutes = 60          # Tempo de aprendizado RT
rt_price_spike_threshold = 0.003  # 0.3% (mais sensível)
rt_volume_spike_threshold = 3.0   # 3× média
rt_velocity_threshold = 0.001     # 0.1%/segundo

partial_sell_ratio = 0.50         # 50% vendido no sinal RT
partial_sell_timeout_sec = 300    # 5 min aguardando 1h
```

### Ajustes recomendados por ativo

| Parâmetro | PAXG | BTC/ETH | Altcoins |
|---|---|---|---|
| `price_spike_threshold` | 0.005 | 0.010 | 0.015 |
| `volume_spike_threshold` | 5.0 | 8.0 | 10.0 |
| `rt_price_spike_threshold` | 0.003 | 0.005 | 0.008 |
| `rt_volume_spike_threshold` | 3.0 | 5.0 | 8.0 |
| `partial_sell_ratio` | 0.50 | 0.30 | 0.50 |

---

## 12. Histórico de Versões

| Versão | Data | Novidades |
|---|---|---|
| v4.3.5 | — | Base: DCA + trailing stop |
| v5.0.3 | — | Backtest engine HTML + export config |
| v5.1.0 | 28/02/2026 | [M1] Filtro RSI, [M2] Volume, [M3] Circuit Breaker, [M4] Stop Loss USD, [M5] ATR, [M6] Detector velas 1h |
| v5.2.0 | 02/03/2026 | Detector Híbrido: RT tick-by-tick + venda parcial inteligente + WebSocket @trade |

### Melhorias M1–M6 (v5.1.0)

| ID | Nome | Benefício |
|---|---|---|
| M1 | Filtro RSI | Evita comprar no topo (RSI 1h < 65) |
| M2 | Confirmação de Volume | Exige liquidez real na entrada |
| M3 | Circuit Breaker | Pausa em quedas ≥ 3% em 4h |
| M4 | Stop Loss USD | Limita perda máxima absoluta por ciclo |
| M5 | Trailing ATR | Adapta saída à volatilidade real |
| M6 | Anomaly Detector | Captura flash pumps/crashes em velas 1h |

### Novidade v5.2.0 — Híbrido

| Aspecto | v5.1.0 | v5.2.0 |
|---|---|---|
| Detecção de pump | Após vela fechar (≤1h) | Durante o evento (10-15s) |
| WebSocket | Apenas @ticker | @ticker + @trade |
| Venda parcial | Não | Sim (50% RT + 50% confirmado) |
| Fonte de dados RT | — | Ticks em tempo real |
| Baseline RT | — | 60 min de aprendizado |
| Velocidade | — | Detecta por %/seg |

---

## Dependências

```
ccxt          # Conexão Binance
pandas        # Manipulação de dados OHLCV
numpy         # Cálculos estatísticos (ATR, RSI)
websockets    # WebSocket @ticker e @trade
colorama      # Dashboard colorido no terminal
configparser  # Leitura do config.ini
```

---

*Sniper Phoenix v5.2.0 — "Detecção híbrida em tempo real"*  
*Documentação gerada em 02/03/2026*
