# =====================================================================================
# SNIPER PHOENIX v5.1.0 - COM DETECTOR DE ANOMALIAS
#
# Base: v4.3.5 Refatorado (todos os bugfixes aplicados)
#
# Melhorias estratégicas:
#   [M1] Filtro RSI na entrada — evita comprar no topo (RSI 1h < 65)
#   [M2] Confirmação de Volume — exige volume acima da média na entrada
#   [M3] Circuit Breaker de mercado — pausa entradas em quedas fortes (> 3% em 4h)
#   [M4] Stop Loss dinâmico em USD — protege o capital total, não só pós-DCA
#   [M5] Trailing Stop adaptativo com ATR — se ajusta à volatilidade real do ativo
# =====================================================================================

import ccxt
import pandas as pd
import time
import configparser
import os
import sys
import threading
from datetime import datetime
from colorama import Fore, init
import signal
import asyncio
import websockets
import json
import logging
import csv
import numpy as np
from collections import deque

init(autoreset=True)
inicio_bot = datetime.now()

# --- CONFIGURAÇÃO DE ARQUIVOS ---
ARQUIVO_ESTADO     = "sniper_state.json"
ARQUIVO_LOG_SISTEMA = "sniper_system.log"
ARQUIVO_LOG_TRADES  = "sniper_trades.csv"

# --- LOCK GLOBAL ---
state_lock = threading.Lock()

# =============================================================================
# [M6] DETECTOR DE ANOMALIAS (FLASH PUMP/CRASH)
# =============================================================================
class AnomalyDetector:
    """
    Detecta flash pumps (altas súbitas) e flash crashes (quedas súbitas)
    usando análise estatística de preço, volume e trades.
    """
    
    def __init__(self, config):
        self.ENABLED = config.get("ANOMALY_DETECTOR_ENABLED", True)
        self.PRICE_SPIKE_THRESHOLD = config.get("PRICE_SPIKE_THRESHOLD", 0.005)
        self.VOLUME_SPIKE_THRESHOLD = config.get("VOLUME_SPIKE_THRESHOLD", 5.0)
        self.TRADES_SPIKE_THRESHOLD = config.get("TRADES_SPIKE_THRESHOLD", 10.0)
        self.LOOKBACK_CANDLES = config.get("ANOMALY_LOOKBACK", 20)
        
        self.price_history = deque(maxlen=self.LOOKBACK_CANDLES)
        self.volume_history = deque(maxlen=self.LOOKBACK_CANDLES)
        self.trades_history = deque(maxlen=self.LOOKBACK_CANDLES)
        
        self.flash_pumps_detected = 0
        self.flash_crashes_detected = 0
        self.emergency_sells = 0
        self.emergency_buys = 0
        
        self.MIN_CONFIDENCE_SELL = config.get("EMERGENCY_SELL_MIN_CONFIDENCE", 0.70)
        self.MIN_CONFIDENCE_BUY = config.get("EMERGENCY_BUY_MIN_CONFIDENCE", 0.80)
    
    def update(self, candle_data):
        """Atualiza históricos com nova vela (1h)."""
        if not self.ENABLED:
            return
        try:
            self.price_history.append(float(candle_data['c']))
            self.volume_history.append(float(candle_data['v']))
            trades = candle_data.get('trades', float(candle_data['v']) * 100)
            self.trades_history.append(float(trades))
        except Exception as e:
            logging.warning(f"Erro ao atualizar detector: {e}")
    
    def detect_flash_pump(self, current_candle):
        """Detecta flash pump (alta anômala súbita)."""
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_spike = ((float(current_candle['h']) / float(current_candle['o'])) - 1)
            if price_spike < self.PRICE_SPIKE_THRESHOLD:
                return None
            
            avg_volume = np.mean(self.volume_history)
            avg_trades = np.mean(self.trades_history)
            current_volume = float(current_candle['v'])
            current_trades = current_candle.get('trades', current_volume * 100)
            
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            trades_ratio = current_trades / avg_trades if avg_trades > 0 else 1
            
            if volume_ratio < self.VOLUME_SPIKE_THRESHOLD or trades_ratio < self.TRADES_SPIKE_THRESHOLD:
                return None
            
            confidence = min(price_spike / 0.01, volume_ratio / 10, trades_ratio / 20, 1.0)
            self.flash_pumps_detected += 1
            
            return {
                "type": "FLASH_PUMP",
                "spike_pct": price_spike * 100,
                "volume_ratio": volume_ratio,
                "trades_ratio": trades_ratio,
                "confidence": confidence,
                "price_peak": float(current_candle['h'])
            }
        except Exception as e:
            logging.error(f"Erro ao detectar flash pump: {e}")
            return None
    
    def detect_flash_crash(self, current_candle):
        """Detecta flash crash (queda anômala súbita)."""
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_drop = ((float(current_candle['o']) / float(current_candle['l'])) - 1)
            if price_drop < self.PRICE_SPIKE_THRESHOLD:
                return None
            
            avg_volume = np.mean(self.volume_history)
            avg_trades = np.mean(self.trades_history)
            current_volume = float(current_candle['v'])
            current_trades = current_candle.get('trades', current_volume * 100)
            
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
            trades_ratio = current_trades / avg_trades if avg_trades > 0 else 1
            
            if volume_ratio < self.VOLUME_SPIKE_THRESHOLD or trades_ratio < self.TRADES_SPIKE_THRESHOLD:
                return None
            
            confidence = min(price_drop / 0.01, volume_ratio / 10, trades_ratio / 20, 1.0)
            self.flash_crashes_detected += 1
            
            return {
                "type": "FLASH_CRASH",
                "drop_pct": price_drop * 100,
                "volume_ratio": volume_ratio,
                "trades_ratio": trades_ratio,
                "confidence": confidence,
                "price_bottom": float(current_candle['l'])
            }
        except Exception as e:
            logging.error(f"Erro ao detectar flash crash: {e}")
            return None
    
    def should_emergency_sell(self, anomaly, in_position, profit_pct):
        """Decide se deve vender imediatamente em flash pump."""
        if not in_position or anomaly is None or anomaly['type'] != 'FLASH_PUMP':
            return False
        if anomaly['confidence'] < self.MIN_CONFIDENCE_SELL or profit_pct < 0:
            return False
        self.emergency_sells += 1
        return True
    
    def should_emergency_buy(self, anomaly, in_position, capital_available):
        """Decide se deve comprar imediatamente em flash crash."""
        if in_position or anomaly is None or anomaly['type'] != 'FLASH_CRASH':
            return False
        if anomaly['confidence'] < self.MIN_CONFIDENCE_BUY or capital_available <= 0:
            return False
        self.emergency_buys += 1
        return True



# =============================================================================
# SISTEMA DE AUDITORIA
# =============================================================================
class Auditoria:
    @staticmethod
    def configurar():
        logging.basicConfig(
            filename=ARQUIVO_LOG_SISTEMA,
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        if not os.path.exists(ARQUIVO_LOG_TRADES):
            with open(ARQUIVO_LOG_TRADES, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    "DATA", "PAR", "TIPO", "PRECO", "QTD",
                    "TOTAL_USD", "LUCRO_USD", "LUCRO_PERC", "SALDO_VAULT", "OBS"
                ])

    @staticmethod
    def log_sistema(msg, nivel="INFO"):
        getattr(logging, {"INFO":"info","AVISO":"warning",
                          "ERRO":"error","CRITICO":"critical"}.get(nivel,"info"))(msg)
        return msg

    @staticmethod
    def log_transacao(tipo, preco, qtd, total_usd,
                      lucro_usd=0.0, lucro_perc=0.0, saldo_vault=0.0, obs=""):
        try:
            with open(ARQUIVO_LOG_TRADES, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    CONFIG["SYMBOL"], tipo,
                    f"{preco:.8f}", f"{qtd:.8f}", f"{total_usd:.2f}",
                    f"{lucro_usd:.2f}", f"{lucro_perc:.2f}%",
                    f"{saldo_vault:.2f}", obs
                ])
        except Exception as e:
            logging.error(f"Erro CSV: {e}")


# =============================================================================
# ESTADO COMPARTILHADO
# =============================================================================
shared_state = {
    "preco": 0.0,
    "erros_consecutivos": 0,
    "marcha": "INICIALIZANDO...",
    "mtf": "INICIALIZANDO...",
    "filtros": {},
    "em_operacao": False,
    "preco_medio": 0.0,
    "lucro_perc_atual": 0.0,
    "perda_usd_atual": 0.0,       # [M4] perda absoluta em USD visível no dashboard
    "proxima_compra_p": 0.0,
    "num_safety_orders": 0,
    "alvo_trailing_ativacao": 0.0,
    "stop_atual_trailing": 0.0,
    "trailing_ativo": False,
    "max_p_trailing": 0.0,
    "trailing_dist_atual": 0.0,   # [M5] distância ATR atual visível no dashboard
    "circuit_breaker": False,
    # [M6] Estado do detector
    "anomaly_flash_pumps": 0,
    "anomaly_flash_crashes": 0,
    "anomaly_emergency_sells": 0,
    "anomaly_emergency_buys": 0,      # [M3] flag pública
    "rsi_atual": 0.0,              # [M1] RSI atual visível no dashboard
    "msg_log": "Sistema Iniciado",
}

menu_ativo      = False
exchange        = None
ordens_executadas = []

# [M6] Instância global do detector
anomaly_detector = None  # protegido por state_lock


# =============================================================================
# CONFIGURAÇÃO GLOBAL
# =============================================================================
CONFIG = {
    "API_KEY": "",
    "SECRET":  "",
    "SYMBOL":    "BTC/USDT",
    "MOEDA_BASE": "USDT",

    # Capital
    "CAPITAL_BASE":      14.75,
    "MAX_SAFETY_ORDERS": 3,

    # DCA
    "DCA_VOLUME_SCALE": 1.5,
    "DCA_STEP_INITIAL": 0.02,
    "DCA_STEP_SCALE":   1.3,

    # Saída
    "TRAILING_TRIGGER": 0.015,   # gatilho de ativação do trailing (1.5%)
    "TRAILING_DIST":    0.005,   # distância mínima do trailing (fallback se ATR falhar)

    # [M4] Stop Loss em valor absoluto USD (substitui o percentual pós-DCA)
    "STOP_LOSS_USD": 10.0,       # nunca perder mais que $10 no ciclo inteiro

    # [M1] Filtro RSI
    "RSI_MAX_ENTRADA": 65.0,     # não entra se RSI(1h) >= 65 (sobrecomprado)
    "RSI_PERIOD":      14,

    # [M2] Filtro de Volume
    "VOLUME_FATOR_MIN": 1.2,     # volume atual deve ser >= 1.2x a média dos últimos 20 candles

    # [M3] Circuit Breaker
    "CB_QUEDA_PCT":    3.0,      # pausa entradas se 4h cair mais de 3%
    "CB_JANELA_VELAS": 5,        # últimas N velas de 4h para medir a queda

    # [M5] ATR para Trailing
    "ATR_PERIOD":      14,
    "ATR_MULTIPLICADOR": 1.5,    # trailing = ATR% * 1.5 (mínimo = TRAILING_DIST)
    
    # [M6] Detector de Anomalias
    "ANOMALY_DETECTOR_ENABLED": True,
    "PRICE_SPIKE_THRESHOLD": 0.005,      # 0.5% (spike mínimo para detecção)
    "VOLUME_SPIKE_THRESHOLD": 5.0,       # 5× média
    "TRADES_SPIKE_THRESHOLD": 10.0,      # 10× média  
    "ANOMALY_LOOKBACK": 20,              # janela de análise (20 velas)
    "EMERGENCY_SELL_MIN_CONFIDENCE": 0.70,   # 70% confiança para venda
    "EMERGENCY_BUY_MIN_CONFIDENCE": 0.80,    # 80% confiança para compra
}


# =============================================================================
# PERSISTÊNCIA
# =============================================================================
def salvar_estado_disco():
    try:
        with state_lock:
            dados = {
                "em_operacao":   shared_state["em_operacao"],
                "ordens":        list(ordens_executadas),
                "symbol":        CONFIG["SYMBOL"],
                "max_p_trailing": shared_state["max_p_trailing"],
                "trailing_ativo": shared_state["trailing_ativo"],
                "timestamp":     str(datetime.now()),
            }
        with open(ARQUIVO_ESTADO, "w") as f:
            json.dump(dados, f, indent=4)
    except Exception as e:
        definir_status(f"Erro ao salvar estado: {e}", "ERRO")


def limpar_estado_disco():
    if os.path.exists(ARQUIVO_ESTADO):
        try:
            os.remove(ARQUIVO_ESTADO)
            Auditoria.log_sistema("Save deletado após ciclo finalizado.", "INFO")
        except Exception:
            pass


def carregar_estado_disco():
    global ordens_executadas
    if not os.path.exists(ARQUIVO_ESTADO):
        return False
    try:
        with open(ARQUIVO_ESTADO) as f:
            dados = json.load(f)
        if dados.get("em_operacao") and dados.get("symbol") == CONFIG["SYMBOL"]:
            with state_lock:
                ordens_executadas            = dados["ordens"]
                shared_state["em_operacao"]  = True
                shared_state["marcha"]       = "RECUPERANDO..."
                shared_state["max_p_trailing"] = dados.get("max_p_trailing", 0.0)
                shared_state["trailing_ativo"] = dados.get("trailing_ativo", False)
            Auditoria.log_sistema(f"ESTADO RESTAURADO: {len(ordens_executadas)} ordens.", "AVISO")
            return True
    except Exception as e:
        Auditoria.log_sistema(f"Erro ao ler save: {e}", "ERRO")
    return False


# =============================================================================
# CONEXÃO / CONFIG
# =============================================================================
def carregar_configuracoes():
    global exchange, CONFIG
    Auditoria.configurar()
    msg_update = ""
    mudanca_symbol = False

    if os.path.exists("config.ini"):
        cp = configparser.ConfigParser()
        cp.read("config.ini")
        try:
            CONFIG["API_KEY"] = cp["binance"]["api_key"]
            CONFIG["SECRET"]  = cp["binance"]["secret"]

            novo_symbol = cp["trading"]["symbol"]
            if novo_symbol != CONFIG["SYMBOL"]:
                mudanca_symbol = True
                msg_update += f" [Novo Par: {novo_symbol}]"
            CONFIG["SYMBOL"]     = novo_symbol
            CONFIG["MOEDA_BASE"] = CONFIG["SYMBOL"].split('/')[1]

            g = cp["trading"].get
            CONFIG["CAPITAL_BASE"]      = float(g("capital_base",      CONFIG["CAPITAL_BASE"]))
            CONFIG["MAX_SAFETY_ORDERS"] = int(  g("max_safety_orders", CONFIG["MAX_SAFETY_ORDERS"]))
            CONFIG["DCA_VOLUME_SCALE"]  = float(g("dca_volume_scale",  CONFIG["DCA_VOLUME_SCALE"]))
            CONFIG["DCA_STEP_INITIAL"]  = float(g("dca_step_initial",  CONFIG["DCA_STEP_INITIAL"]))
            CONFIG["DCA_STEP_SCALE"]    = float(g("dca_step_scale",    CONFIG["DCA_STEP_SCALE"]))
            CONFIG["TRAILING_TRIGGER"]  = float(g("trailing_trigger",  CONFIG["TRAILING_TRIGGER"]))
            CONFIG["TRAILING_DIST"]     = float(g("trailing_dist",     CONFIG["TRAILING_DIST"]))
            # Novas configs
            CONFIG["STOP_LOSS_USD"]       = float(g("stop_loss_usd",       CONFIG["STOP_LOSS_USD"]))
            CONFIG["RSI_MAX_ENTRADA"]     = float(g("rsi_max_entrada",     CONFIG["RSI_MAX_ENTRADA"]))
            CONFIG["RSI_PERIOD"]          = int(  g("rsi_period",          CONFIG["RSI_PERIOD"]))
            CONFIG["VOLUME_FATOR_MIN"]    = float(g("volume_fator_min",    CONFIG["VOLUME_FATOR_MIN"]))
            CONFIG["CB_QUEDA_PCT"]        = float(g("cb_queda_pct",        CONFIG["CB_QUEDA_PCT"]))
            CONFIG["CB_JANELA_VELAS"]     = int(  g("cb_janela_velas",     CONFIG["CB_JANELA_VELAS"]))
            CONFIG["ATR_PERIOD"]          = int(  g("atr_period",          CONFIG["ATR_PERIOD"]))
            CONFIG["ATR_MULTIPLICADOR"]   = float(g("atr_multiplicador",   CONFIG["ATR_MULTIPLICADOR"]))
            
            # [M6] Detector de Anomalias
            if "anomaly" in cp:
                ga = cp["anomaly"].get
                CONFIG["ANOMALY_DETECTOR_ENABLED"]       = cp["anomaly"].getboolean("enabled", CONFIG["ANOMALY_DETECTOR_ENABLED"])
                CONFIG["PRICE_SPIKE_THRESHOLD"]          = float(ga("price_spike_threshold", CONFIG["PRICE_SPIKE_THRESHOLD"]))
                CONFIG["VOLUME_SPIKE_THRESHOLD"]         = float(ga("volume_spike_threshold", CONFIG["VOLUME_SPIKE_THRESHOLD"]))
                CONFIG["TRADES_SPIKE_THRESHOLD"]         = float(ga("trades_spike_threshold", CONFIG["TRADES_SPIKE_THRESHOLD"]))
                CONFIG["ANOMALY_LOOKBACK"]               = int(  ga("lookback_candles", CONFIG["ANOMALY_LOOKBACK"]))
                CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"]  = float(ga("emergency_sell_min_confidence", CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"]))
                CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]   = float(ga("emergency_buy_min_confidence", CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]))
        except Exception as e:
            Auditoria.log_sistema(f"Erro ao ler config.ini: {e}", "ERRO")

    if exchange is None:
        try:
            print(f"{Fore.YELLOW}Conectando a Binance...")
            exchange = ccxt.binance({
                'apiKey': CONFIG["API_KEY"],
                'secret': CONFIG["SECRET"],
                'enableRateLimit': True,
                'options': {'adjustForTimeDifference': True},
            })
            exchange.load_markets()
            definir_status(f"Conectado: {CONFIG['SYMBOL']}", "SUCESSO")
        except Exception as e:
            print(f"Erro Crítico: {e}")
            sys.exit(1)
    else:
        if mudanca_symbol:
            with state_lock:
                shared_state["filtros"] = {}
                shared_state["preco"]   = 0.0
                shared_state["marcha"]  = "AGUARDANDO NOVO PAR"
        msg = f"Hot Reload:{msg_update}" if msg_update else "Config recarregada (sem alterações)"
        definir_status(msg, "SUCESSO" if msg_update else "INFO")
    
    # [M6] Inicializa/reinicializa detector de anomalias
    global anomaly_detector
    anomaly_detector = AnomalyDetector(CONFIG)
    Auditoria.log_sistema(f"Detector de anomalias: {'ATIVO' if CONFIG['ANOMALY_DETECTOR_ENABLED'] else 'DESATIVADO'}", "INFO")


# =============================================================================
# UTILITÁRIOS
# =============================================================================
def definir_status(msg, tipo="INFO"):
    hora = datetime.now().strftime("%H:%M:%S")
    cor  = {"SUCESSO": Fore.GREEN, "ERRO": Fore.RED, "AVISO": Fore.YELLOW}.get(tipo, Fore.CYAN)
    with state_lock:
        shared_state["msg_log"] = f"{Fore.WHITE}[{hora}] {cor}{msg}"
    if tipo == "ERRO":    Auditoria.log_sistema(msg, "ERRO")
    elif tipo == "SUCESSO": Auditoria.log_sistema(msg, "INFO")


def panico_sistema(mensagem):
    logging.critical(f"DISJUNTOR ATIVADO: {mensagem}")
    definir_status(f"ERRO CRÍTICO: {mensagem} — DESLIGANDO", "ERRO")
    salvar_estado_disco()
    os._exit(1)


# =============================================================================
# INDICADORES TÉCNICOS
# =============================================================================
def calcular_rsi(df: pd.DataFrame, period: int) -> float:
    """[M1] Retorna o RSI mais recente da série de fechamentos."""
    delta = df['c'].diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, float('nan'))
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calcular_atr(df: pd.DataFrame, period: int) -> float:
    """[M5] Retorna o ATR mais recente como percentual do preço atual."""
    high_low   = df['h'] - df['l']
    high_close = (df['h'] - df['c'].shift()).abs()
    low_close  = (df['l'] - df['c'].shift()).abs()
    tr  = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
    preco_atual = df['c'].iloc[-1]
    return float(atr / preco_atual) if preco_atual > 0 else CONFIG["TRAILING_DIST"]


def trailing_dist_por_atr(df_1h: pd.DataFrame) -> float:
    """
    [M5] Calcula a distância do Trailing Stop baseada no ATR.
    Garante que nunca seja menor que TRAILING_DIST (fallback de segurança).
    """
    try:
        atr_perc = calcular_atr(df_1h, CONFIG["ATR_PERIOD"])
        dist     = atr_perc * CONFIG["ATR_MULTIPLICADOR"]
        return max(dist, CONFIG["TRAILING_DIST"])
    except Exception:
        return CONFIG["TRAILING_DIST"]


# =============================================================================
# CÉREBRO: ANÁLISE MTF + FILTROS
# =============================================================================
def check_mtf_trend() -> bool:
    """
    Analisa tendência em 3 timeframes via EMA.
    Atualiza shared_state['filtros'] com os resultados visuais.
    """
    config_tf     = {'15m': 25, '1h': 50, '4h': 100}
    filtros       = {}
    votos_positivos = 0
    try:
        for tf, period in config_tf.items():
            ohlcv = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe=tf, limit=period + 5)
            df    = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
            ema   = df['c'].ewm(span=period, adjust=False).mean()
            if df['c'].iloc[-1] > ema.iloc[-1]:
                filtros[tf] = f"{Fore.GREEN}▲"
                votos_positivos += 1
            else:
                filtros[tf] = f"{Fore.RED}▼"
        with state_lock:
            shared_state["filtros"] = filtros
        return votos_positivos >= 2
    except Exception as e:
        Auditoria.log_sistema(f"Erro MTF: {e}", "ERRO")
        return False


def verificar_circuit_breaker() -> bool:
    """
    [M3] Retorna True se o mercado estiver em queda forte nas últimas N velas de 4h.
    Nesse caso, novas entradas são bloqueadas.
    """
    try:
        janela = CONFIG["CB_JANELA_VELAS"] + 2
        ohlcv  = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe='4h', limit=janela)
        df     = pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
        preco_inicio = float(df['c'].iloc[-(CONFIG["CB_JANELA_VELAS"] + 1)])
        preco_atual  = float(df['c'].iloc[-1])
        variacao_pct = ((preco_atual / preco_inicio) - 1) * 100

        ativado = variacao_pct <= -CONFIG["CB_QUEDA_PCT"]
        with state_lock:
            shared_state["circuit_breaker"] = ativado

        if ativado:
            definir_status(
                f"⚡ Circuit Breaker: mercado caiu {variacao_pct:.2f}% em "
                f"{CONFIG['CB_JANELA_VELAS']} velas de 4h. Entradas pausadas.", "AVISO")
        return ativado
    except Exception as e:
        Auditoria.log_sistema(f"Erro Circuit Breaker: {e}", "ERRO")
        return False  # na dúvida, não bloqueia


def verificar_filtros_entrada(df_1h: pd.DataFrame) -> tuple[bool, str]:
    """
    [M1 + M2] Verifica RSI e Volume antes de abrir posição.
    Retorna (aprovado: bool, motivo: str).
    """
    # --- RSI [M1] ---
    try:
        rsi = calcular_rsi(df_1h, CONFIG["RSI_PERIOD"])
        with state_lock:
            shared_state["rsi_atual"] = rsi
        if rsi >= CONFIG["RSI_MAX_ENTRADA"]:
            return False, f"RSI sobrecomprado ({rsi:.1f} >= {CONFIG['RSI_MAX_ENTRADA']})"
    except Exception as e:
        Auditoria.log_sistema(f"Erro filtro RSI: {e}", "AVISO")

    # --- Volume [M2] ---
    try:
        vol_atual = float(df_1h['v'].iloc[-1])
        vol_media = float(df_1h['v'].rolling(20).mean().iloc[-1])
        if vol_atual < vol_media * CONFIG["VOLUME_FATOR_MIN"]:
            return False, (f"Volume baixo ({vol_atual:.0f} < "
                           f"{vol_media * CONFIG['VOLUME_FATOR_MIN']:.0f})")
    except Exception as e:
        Auditoria.log_sistema(f"Erro filtro Volume: {e}", "AVISO")

    return True, "OK"


def obter_df_1h(limit: int = 120) -> pd.DataFrame | None:
    """Busca candles de 1h e retorna DataFrame pronto para análise."""
    try:
        ohlcv = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe='1h', limit=limit)
        return pd.DataFrame(ohlcv, columns=['t','o','h','l','c','v'])
    except Exception as e:
        Auditoria.log_sistema(f"Erro ao buscar candles 1h: {e}", "ERRO")
        return None


# =============================================================================
# CÁLCULOS
# =============================================================================
def calcular_preco_medio() -> float:
    with state_lock:
        ordens = list(ordens_executadas)
    total_vol   = sum(o['volume'] for o in ordens)
    total_custo = sum(o['custo']  for o in ordens)
    return total_custo / total_vol if total_vol > 0 else 0


def calcular_proxima_safety_order(preco_ult: float, num_ordem_atual: int):
    if num_ordem_atual >= CONFIG["MAX_SAFETY_ORDERS"]:
        return None, None
    with state_lock:
        custo_anterior = ordens_executadas[-1]['custo']
    novo_vol_usd    = custo_anterior * CONFIG["DCA_VOLUME_SCALE"]
    fator_distancia = CONFIG["DCA_STEP_SCALE"] ** num_ordem_atual
    pct_queda       = CONFIG["DCA_STEP_INITIAL"] * fator_distancia
    novo_preco      = preco_ult * (1 - pct_queda)
    return novo_preco, novo_vol_usd


# =============================================================================
# VAULT
# =============================================================================
def get_saldo_fundos() -> float:
    try:
        bal = exchange.fetch_balance({'type': 'funding'})
        return bal.get(CONFIG["MOEDA_BASE"], {}).get('free', 0)
    except Exception:
        return 0


def transferir_para_spot(valor: float) -> bool:
    try:
        exchange.transfer(CONFIG["MOEDA_BASE"], valor, 'funding', 'spot')
        Auditoria.log_sistema(f"VAULT: ${valor:.2f} enviado ao Spot", "INFO")
        time.sleep(1)
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Fundos→Spot): {e}", "ERRO")
        return False


def recolher_para_fundos() -> bool:
    try:
        balance    = exchange.fetch_balance()
        saldo_usdt = balance.get(CONFIG["MOEDA_BASE"], {}).get('free', 0)
        if saldo_usdt > 0.5:
            exchange.transfer(CONFIG["MOEDA_BASE"], saldo_usdt, 'spot', 'funding')
            Auditoria.log_sistema(f"VAULT: ${saldo_usdt:.2f} protegido em Fundos", "INFO")
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Spot→Fundos): {e}", "ERRO")
        return False


def inicializar_vault():
    definir_status("Auditando Vault...", "INFO")
    recolher_para_fundos()
    try:
        saldo = get_saldo_fundos()
        nivel = "AVISO" if saldo < 120 else "SUCESSO"
        definir_status(f"Vault: ${saldo:.2f}", nivel)
    except Exception as e:
        definir_status(f"Erro Vault Init: {e}", "ERRO")


# =============================================================================
# EXECUÇÃO DE ORDENS
# =============================================================================
def comprar_com_vault(valor_usd: float, motivo: str = "Entrada") -> dict:
    if not transferir_para_spot(valor_usd):
        return {'ok': False, 'msg': 'Falha na transferência Fundos→Spot'}
    try:
        ordem = exchange.create_order(
            CONFIG['SYMBOL'], 'market', 'buy', None,
            params={'quoteOrderQty': exchange.cost_to_precision(CONFIG['SYMBOL'], valor_usd)}
        )
        with state_lock:
            shared_state["erros_consecutivos"] = 0
        preco_exec = float(ordem.get('average') or ordem.get('price') or 0)
        res = {'ok': True, 'p': preco_exec,
               'v': float(ordem['amount']), 'c': float(ordem['cost'])}
        Auditoria.log_transacao("COMPRA", res['p'], res['v'], res['c'], obs=motivo)
        return res
    except Exception as e:
        with state_lock:
            shared_state["erros_consecutivos"] += 1
            cnt = shared_state["erros_consecutivos"]
        logging.error(f"Erro Compra: {e} ({cnt}/5)")
        definir_status("Ordem falhou! Recolhendo ao Vault...", "AVISO")
        recolher_para_fundos()
        return {'ok': False, 'msg': str(e)}


def executar_venda_mercado(qtd: float, motivo: str = "Saida") -> dict:
    try:
        if not exchange.markets:
            exchange.load_markets()
        qtd_fmt = exchange.amount_to_precision(CONFIG['SYMBOL'], qtd)
        ordem   = exchange.create_order(CONFIG['SYMBOL'], 'market', 'sell', qtd_fmt)
        preco_exec = float(ordem.get('average') or ordem.get('price') or 0)
        custo_total = float(ordem.get('cost') or 0)
        return {'ok': True, 'p': preco_exec, 'c': custo_total}
    except Exception as e:
        Auditoria.log_sistema(f"Erro Venda ({motivo}): {e}", "ERRO")
        return {'ok': False, 'msg': str(e)}


# =============================================================================
# FINALIZAÇÃO DE CICLO
# =============================================================================
def finalizar_ciclo(motivo: str = "SUCESSO"):
    recolher_para_fundos()
    with state_lock:
        ordens_executadas.clear()
        shared_state["em_operacao"]           = False
        shared_state["trailing_ativo"]        = False
        shared_state["max_p_trailing"]        = 0.0
        shared_state["alvo_trailing_ativacao"] = 0.0
        shared_state["stop_atual_trailing"]   = 0.0
        shared_state["perda_usd_atual"]       = 0.0
        shared_state["trailing_dist_atual"]   = 0.0
    limpar_estado_disco()
    Auditoria.log_sistema(f"Ciclo encerrado. Motivo: {motivo}", "INFO")


# =============================================================================
# LOOP DCA ATIVO
# =============================================================================
def loop_dca_ativo():
    with state_lock:
        shared_state["em_operacao"] = True
        shared_state["marcha"]      = "DCA ATIVO"
        trailing_ativo = shared_state.get("trailing_ativo", False)
        max_p          = shared_state.get("max_p_trailing", 0.0)
        ordens_snap    = list(ordens_executadas)

    prox_p, prox_v_usd = calcular_proxima_safety_order(
        ordens_snap[-1]['p'], len(ordens_snap) - 1)

    # [M5] Calcula trailing dist com ATR na abertura do ciclo
    trailing_dist = CONFIG["TRAILING_DIST"]
    df_1h_inicial = obter_df_1h()
    if df_1h_inicial is not None:
        trailing_dist = trailing_dist_por_atr(df_1h_inicial)
    with state_lock:
        shared_state["trailing_dist_atual"] = trailing_dist

    while True:
        with state_lock:
            erros = shared_state.get("erros_consecutivos", 0)
        if erros >= 5:
            panico_sistema("Múltiplos erros consecutivos detectados.")
            break

        try:
            # [M6] Atualiza detector com vela atual
            df_1h_atual = obter_df_1h(limit=25)
            if df_1h_atual is not None and len(df_1h_atual) > 0:
                candle_atual = {
                    'o': df_1h_atual['o'].iloc[-1],
                    'h': df_1h_atual['h'].iloc[-1],
                    'l': df_1h_atual['l'].iloc[-1],
                    'c': df_1h_atual['c'].iloc[-1],
                    'v': df_1h_atual['v'].iloc[-1]
                }
                anomaly_detector.update(candle_atual)
                
                # Atualiza estatísticas do detector no dashboard
                with state_lock:
                    shared_state["anomaly_flash_pumps"] = anomaly_detector.flash_pumps_detected
                    shared_state["anomaly_flash_crashes"] = anomaly_detector.flash_crashes_detected
                    shared_state["anomaly_emergency_sells"] = anomaly_detector.emergency_sells
                    shared_state["anomaly_emergency_buys"] = anomaly_detector.emergency_buys
            

            with state_lock:
                em_op = shared_state["em_operacao"]
                preco = shared_state["preco"]

            if not em_op:
                break
            if preco <= 0:
                time.sleep(0.5)
                continue

            pm              = calcular_preco_medio()
            lucro_atual_perc = ((preco / pm) - 1) * 100

            with state_lock:
                n_ordens = len(ordens_executadas)
                custo_total_ciclo = sum(o['custo'] for o in ordens_executadas)
                qtd_total_ciclo   = sum(o['volume'] for o in ordens_executadas)

            valor_atual_ciclo = preco * qtd_total_ciclo
            perda_usd         = custo_total_ciclo - valor_atual_ciclo  # positivo = prejuízo

            with state_lock:
                shared_state["preco_medio"]        = pm
                shared_state["lucro_perc_atual"]   = lucro_atual_perc
                shared_state["perda_usd_atual"]    = perda_usd
                shared_state["num_safety_orders"]  = n_ordens - 1
                shared_state["proxima_compra_p"]   = prox_p if prox_p else 0.0
                shared_state["max_p_trailing"]     = max_p

            # -------------------------------------------------------------------
            # 1. GESTÃO DE COMPRA (DCA)
            # -------------------------------------------------------------------
            if prox_p and preco <= prox_p:
                msg_ordem = f"DCA #{n_ordens}"
                definir_status(f"Executando {msg_ordem}...", "AVISO")
                res = comprar_com_vault(prox_v_usd, motivo=msg_ordem)
                if res['ok']:
                    with state_lock:
                        ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                        n_ordens = len(ordens_executadas)
                    trailing_ativo = False
                    max_p          = 0.0

                    # [M5] Recalcula trailing dist com ATR após novo DCA
                    df_1h_novo = obter_df_1h()
                    if df_1h_novo is not None:
                        trailing_dist = trailing_dist_por_atr(df_1h_novo)
                        with state_lock:
                            shared_state["trailing_dist_atual"] = trailing_dist

                    salvar_estado_disco()
                    prox_p, prox_v_usd = calcular_proxima_safety_order(res['p'], n_ordens - 1)
                else:
                    definir_status(f"Falha DCA: {res.get('msg','Erro desconhecido')}", "ERRO")

            # -------------------------------------------------------------------
            # 2. [M4] STOP LOSS DINÂMICO EM USD
            # Verifica perda máxima em USD a qualquer momento do ciclo,
            # independentemente de quantas Safety Orders foram usadas.
            # -------------------------------------------------------------------
            if perda_usd >= CONFIG["STOP_LOSS_USD"]:
                definir_status(
                    f"🛑 STOP LOSS USD: prejuízo ${perda_usd:.2f} >= "
                    f"${CONFIG['STOP_LOSS_USD']:.2f}", "ERRO")
                res_venda = executar_venda_mercado(qtd_total_ciclo, motivo="SL USD")
                if res_venda['ok']:
                    Auditoria.log_transacao(
                        "STOP LOSS", res_venda['p'], qtd_total_ciclo, res_venda['c'],
                        lucro_usd=-perda_usd,
                        lucro_perc=lucro_atual_perc,
                        obs=f"SL USD (limite ${CONFIG['STOP_LOSS_USD']:.2f})")
                    finalizar_ciclo(motivo="STOP LOSS USD")
                    break

            
            # -------------------------------------------------------------------
            # 2.5. [M6] DETECÇÃO DE ANOMALIAS (FLASH PUMP/CRASH)
            # Verifica ANTES do trailing para capturar picos instantâneos
            # -------------------------------------------------------------------
            if anomaly_detector.ENABLED and df_1h_atual is not None:
                flash_pump = anomaly_detector.detect_flash_pump(candle_atual)
                
                if flash_pump:
                    # Venda de emergência em flash pump
                    if anomaly_detector.should_emergency_sell(flash_pump, True, lucro_atual_perc):
                        definir_status(
                            f"🚨 FLASH PUMP! Spike: {flash_pump['spike_pct']:.2f}% | "
                            f"Vol: {flash_pump['volume_ratio']:.0f}× | "
                            f"Conf: {flash_pump['confidence']:.0%}", "ERRO")
                        
                        # Vende no pico (98% do high para garantir execução)
                        preco_venda_emergencia = flash_pump['price_peak'] * 0.998
                        
                        res_venda = executar_venda_mercado(qtd_total_ciclo, motivo="FLASH PUMP")
                        if res_venda['ok']:
                            Auditoria.log_transacao(
                                tipo="EMERGÊNCIA PUMP",
                                preco=res_venda['p'],
                                qtd=qtd_total_ciclo,
                                total_usd=res_venda['c'],
                                lucro_usd=(res_venda['c'] - custo_total_ciclo),
                                lucro_perc=lucro_atual_perc,
                                saldo_vault=get_saldo_fundos(),
                                obs=f"Flash Pump {flash_pump['spike_pct']:.2f}% | Conf {flash_pump['confidence']:.0%}")
                            
                            with state_lock:
                                shared_state["anomaly_emergency_sells"] = anomaly_detector.emergency_sells
                            
                            finalizar_ciclo(motivo="VENDA EMERGÊNCIA FLASH PUMP")
                            break

            # -------------------------------------------------------------------
            # 3. [M5] TRAILING STOP COM ATR
            # -------------------------------------------------------------------
            gatilho_ts = pm * (1 + CONFIG["TRAILING_TRIGGER"])
            with state_lock:
                shared_state["alvo_trailing_ativacao"] = gatilho_ts

            if not trailing_ativo:
                if preco >= gatilho_ts:
                    trailing_ativo = True
                    max_p          = preco
                    with state_lock:
                        shared_state["trailing_ativo"]  = True
                        shared_state["max_p_trailing"]  = max_p
                    definir_status(
                        f"✅ Trailing Ativado! Dist ATR: {trailing_dist*100:.2f}%", "SUCESSO")
            else:
                if preco > max_p:
                    max_p = preco
                    with state_lock:
                        shared_state["max_p_trailing"] = max_p

                stop_price = max_p * (1 - trailing_dist)
                with state_lock:
                    shared_state["stop_atual_trailing"] = stop_price

                if preco <= stop_price:
                    definir_status("🎯 Take Profit via Trailing Stop!", "SUCESSO")
                    res_venda = executar_venda_mercado(qtd_total_ciclo, motivo="TP Trailing")
                    if res_venda['ok']:
                        Auditoria.log_transacao(
                            tipo="TAKE PROFIT",
                            preco=res_venda['p'],
                            qtd=qtd_total_ciclo,
                            total_usd=res_venda['c'],
                            lucro_usd=(res_venda['c'] - custo_total_ciclo),
                            lucro_perc=lucro_atual_perc,
                            saldo_vault=get_saldo_fundos(),
                            obs=f"Trailing ATR {trailing_dist*100:.2f}%")
                        finalizar_ciclo(motivo="TAKE PROFIT")
                        break

            time.sleep(0.5)

        except Exception as e:
            definir_status(f"Erro Loop DCA: {e}", "ERRO")
            time.sleep(2)


# =============================================================================
# THREAD SCANNER MTF
# =============================================================================
def thread_scanner():
    """
    Única responsável por chamar check_mtf_trend() e verificar Circuit Breaker.
    O thread_motor apenas lê shared_state['mtf'] e shared_state['circuit_breaker'].
    """
    while True:
        try:
            cb_ativo   = verificar_circuit_breaker()  # [M3]
            sinal_bool = check_mtf_trend() if not cb_ativo else False
            with state_lock:
                shared_state["mtf"] = "COMPRA" if sinal_bool else "AGUARDANDO"

            # [M1] Atualiza RSI continuamente, independente do sinal ou estado
            df_1h = obter_df_1h()
            if df_1h is not None:
                try:
                    rsi = calcular_rsi(df_1h, CONFIG["RSI_PERIOD"])
                    with state_lock:
                        shared_state["rsi_atual"] = rsi
                except Exception as e:
                    Auditoria.log_sistema(f"Erro RSI Scanner: {e}", "AVISO")

            time.sleep(30)
        except Exception as e:
            logging.error(f"Erro Scanner: {e}")
            time.sleep(10)


# =============================================================================
# THREAD MOTOR
# =============================================================================
def thread_motor():
    if carregar_estado_disco():
        with state_lock:
            shared_state["mtf"] = "RETOMADO"
        loop_dca_ativo()
    else:
        inicializar_vault()

    while True:
        try:
            with state_lock:
                em_op      = shared_state["em_operacao"]
                sinal_mtf  = shared_state["mtf"]
                preco      = shared_state["preco"]
                cb_ativo   = shared_state["circuit_breaker"]

            if not em_op:
                with state_lock:
                    shared_state["marcha"]          = "ESPERANDO MTF"
                    shared_state["preco_medio"]      = 0
                    shared_state["proxima_compra_p"] = 0

                # [M3] Circuit Breaker bloqueia novas entradas
                if cb_ativo:
                    time.sleep(1)
                    continue

                if sinal_mtf == "COMPRA" and preco > 0:
                    # [M1 + M2] Valida RSI e volume antes de entrar
                    df_1h = obter_df_1h()
                    if df_1h is not None:
                        aprovado, motivo_filtro = verificar_filtros_entrada(df_1h)
                        if not aprovado:
                            definir_status(f"Entrada bloqueada: {motivo_filtro}", "AVISO")
                            time.sleep(1)
                            continue

                    definir_status("✅ Sinal confirmado! Abrindo posição...", "SUCESSO")
                    res = comprar_com_vault(CONFIG["CAPITAL_BASE"], motivo="Entrada MTF")
                    if res['ok']:
                        with state_lock:
                            ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                        salvar_estado_disco()
                        loop_dca_ativo()

            time.sleep(1)
            with state_lock:
                shared_state["erros_consecutivos"] = 0

        except Exception as e:
            with state_lock:
                shared_state["erros_consecutivos"] += 1
                cnt = shared_state["erros_consecutivos"]
            logging.error(f"Erro Motor ({cnt}/5): {e}")
            if cnt >= 5:
                panico_sistema("5 erros consecutivos no Motor.")
            time.sleep(5)


# =============================================================================
# THREAD VISUAL
# =============================================================================
def thread_visual():
    while True:
        if not menu_ativo:
            os.system('cls' if os.name == 'nt' else 'clear')
            uptime = str(datetime.now() - inicio_bot).split('.')[0]

            with state_lock:
                s          = dict(shared_state)
                cfg_symbol = CONFIG['SYMBOL']
                cfg_max_so = CONFIG['MAX_SAFETY_ORDERS']
                cfg_sl_usd = CONFIG['STOP_LOSS_USD']

            c = s["preco"]
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v5.1.0 {Fore.WHITE}| ESTRATÉGIA MELHORADA | {Fore.CYAN}UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*80}")

            # Circuit Breaker banner
            if s["circuit_breaker"]:
                print(f"{Fore.RED}  ⚡ CIRCUIT BREAKER ATIVO — Entradas pausadas por queda de mercado")

            filtros = " ".join([f"[{k}:{v}{Fore.WHITE}]" for k, v in s["filtros"].items()])
            rsi_cor = Fore.RED if s["rsi_atual"] >= CONFIG["RSI_MAX_ENTRADA"] else Fore.GREEN
            print(f"  MERCADO: {Fore.GREEN}{cfg_symbol} {Fore.YELLOW}${c:.8f} "
                  f"{Fore.WHITE}| MTF {filtros} | RSI {rsi_cor}{s['rsi_atual']:.1f}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")

            if s["em_operacao"]:
                print(f"{Fore.YELLOW}{'-'*80}")
                pnl     = s['lucro_perc_atual']
                perda   = s['perda_usd_atual']
                cor_pnl = Fore.GREEN if pnl > 0 else Fore.RED
                print(f" P. MÉDIO: {Fore.WHITE}${s['preco_medio']:.8f} "
                      f"{cor_pnl}{pnl:+.2f}%  (${perda:.2f} USD em risco / limite ${cfg_sl_usd:.2f})")

                # Barra de progresso SL USD
                if cfg_sl_usd > 0:
                    progresso_sl = min(perda / cfg_sl_usd, 1.0)
                    blocos_cheios = int(progresso_sl * 20)
                    barra_sl = ("█" * blocos_cheios + "░" * (20 - blocos_cheios))
                    cor_sl   = Fore.GREEN if progresso_sl < 0.5 else (Fore.YELLOW if progresso_sl < 0.8 else Fore.RED)
                    print(f"   SL USD: {cor_sl}[{barra_sl}] {progresso_sl*100:.0f}%")

                usadas  = s['num_safety_orders']
                barra   = "▰" * usadas + "▱" * (cfg_max_so - usadas)
                print(f"   SAFETY: {Fore.YELLOW}{barra} ({usadas}/{cfg_max_so})")

                if s['proxima_compra_p'] > 0:
                    dist_dca = ((s['proxima_compra_p'] / c) - 1) * 100 if c > 0 else 0
                    print(f" DCA PROX: {Fore.RED}${s['proxima_compra_p']:.8f} ({dist_dca:.2f}%)")

                td = s['trailing_dist_atual']
                if s['trailing_ativo']:
                    dist_venda = ((c / s['stop_atual_trailing']) - 1) * 100 \
                        if s['stop_atual_trailing'] > 0 else 0
                    print(f" TRAILING: {Fore.MAGENTA}ATIVO "
                          f"(Stop: ${s['stop_atual_trailing']:.8f} | "
                          f"Recuo: {dist_venda:.2f}% | ATR dist: {td*100:.2f}%)")
                elif s['alvo_trailing_ativacao'] > 0:
                    dist_alvo = ((s['alvo_trailing_ativacao'] / c) - 1) * 100 if c > 0 else 0
                    print(f"  ALVO TS: {Fore.CYAN}${s['alvo_trailing_ativacao']:.8f} "
                          f"(+{dist_alvo:.2f}%) | ATR dist: {td*100:.2f}%")

            print(f"{Fore.YELLOW}{'='*80}")
            print(f" LOG: {s['msg_log']}")
            print(f"{Fore.YELLOW}{'='*80}")
            print(f" {Fore.WHITE}[Ctrl+C] MENU | LOGS: {ARQUIVO_LOG_SISTEMA} | TRADES: {ARQUIVO_LOG_TRADES}")

        time.sleep(1)


# =============================================================================
# MENU
# =============================================================================
def acionar_menu(signum, frame):
    global menu_ativo
    menu_ativo = True
    os.system('cls' if os.name == 'nt' else 'clear')

    print(f"{Fore.MAGENTA}╔══════════════════════════════════════╗")
    print(f"{Fore.MAGENTA}║    MENU DE CONTROLE SNIPER v5.0      ║")
    print(f"{Fore.MAGENTA}╠══════════════════════════════════════╣")
    print(f"{Fore.WHITE}║ 1. VOLTAR AO MONITORAMENTO           ║")
    print(f"{Fore.YELLOW}║ 2. ATUALIZAR CONFIG.INI (HOT RELOAD) ║")
    print(f"{Fore.RED}║ 3. ENCERRAR (DESLIGAR BOT)           ║")
    print(f"{Fore.MAGENTA}╚══════════════════════════════════════╝")

    try:
        opt = input(f"\n{Fore.CYAN}➤ Escolha uma opção [1-3]: {Fore.WHITE}")
        if opt == '1':
            print(f"{Fore.GREEN}Retornando...")
            time.sleep(0.5)
        elif opt == '2':
            carregar_configuracoes()
            print(f"{Fore.GREEN}Configurações aplicadas!")
            time.sleep(2)
        elif opt == '3':
            print(f"{Fore.RED}Salvando dados e encerrando...")
            salvar_estado_disco()
            os._exit(0)
        else:
            print(f"{Fore.RED}Opção inválida.")
            time.sleep(1)
    except Exception as e:
        print(f"Erro no menu: {e}")

    menu_ativo = False


# =============================================================================
# WEBSOCKET
# =============================================================================
async def ws_loop():
    ultimo_symbol = CONFIG['SYMBOL']
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/','').lower()}@ticker"

    while True:
        try:
            if ultimo_symbol != CONFIG['SYMBOL']:
                ultimo_symbol = CONFIG['SYMBOL']
                uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/','').lower()}@ticker"
                with state_lock:
                    shared_state["preco"] = 0.0

            async with websockets.connect(uri) as ws:
                while True:
                    if ultimo_symbol != CONFIG['SYMBOL']:
                        break
                    try:
                        msg   = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        dados = json.loads(msg)
                        with state_lock:
                            shared_state["preco"] = float(dados['c'])
                    except asyncio.TimeoutError:
                        continue
        except Exception:
            await asyncio.sleep(2)


def iniciar_ws():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_loop())


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    signal.signal(signal.SIGINT, acionar_menu)
    carregar_configuracoes()
    threading.Thread(target=thread_motor,   daemon=True, name="Motor").start()
    threading.Thread(target=thread_visual,  daemon=True, name="Visual").start()
    threading.Thread(target=iniciar_ws,     daemon=True, name="WebSocket").start()
    threading.Thread(target=thread_scanner, daemon=True, name="Scanner").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)
