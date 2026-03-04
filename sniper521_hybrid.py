# =====================================================================================
# SNIPER PHOENIX v5.2.1 - DETECTOR HÍBRIDO INTELIGENTE
#
# Base: v5.2.0 (com todas as melhorias M1-M6 + Detector Híbrido)
#
# [v5.2.1] Flags de ativação independente por filtro:
#   • filtro_rsi_ativo      — liga/desliga filtro RSI na entrada [M1]
#   • filtro_volume_ativo   — liga/desliga filtro de volume na entrada [M2]
#   • circuit_breaker_ativo — liga/desliga circuit breaker em quedas [M3]
#
#   Quando FILTRO_RSI_ATIVO = False: RSI ainda é calculado e exibido,
#     mas NÃO bloqueia a entrada.
#   Quando CIRCUIT_BREAKER_ATIVO = False: shared_state["circuit_breaker"]
#     é forçado a False; nenhuma chamada à API de 4h é realizada.
#   Suporte completo a hot-reload via menu opção 2 (Ctrl+C).
#
# [HIBRIDO] Detector Híbrido Inteligente (v5.2.0)
#   Combina dois detectores com pesos diferentes:
#     • RealtimeAnomalyDetector  — tick-by-tick via @trade WebSocket (peso 0.6)
#                                  Latência: 10-15 segundos | Detecção durante o evento
#     • AnomalyDetector (1h)     — velas fechadas via REST (peso 1.0)
#                                  Latência: até 1h | Alta confiança, fallback seguro
#
#   Lógica de decisão:
#     • Só tempo real (score ≥ threshold) → VENDA PARCIAL (50%) + aguarda confirmação
#     • Velas 1h confirmam OU timeout (5 min) → VENDA DO RESTANTE
#     • Velas 1h detectam sozinhas → VENDA TOTAL imediata (comportamento v5.1.0)
#     • Ambos detectam simultaneamente → VENDA TOTAL imediata com score somado
#
#   Para compras (flash crash):
#     • Tempo real detecta crash + confiança ≥ threshold → COMPRA DE EMERGÊNCIA
#     • 1h detecta crash sozinho → COMPRA DE EMERGÊNCIA (comportamento v5.1.0)
# =====================================================================================

import ccxt
import pandas as pd
import time
import configparser
import os
import sys
import threading
from datetime import datetime, timedelta
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
ARQUIVO_ESTADO      = "sniper_state.json"
ARQUIVO_LOG_SISTEMA = "sniper_system.log"
ARQUIVO_LOG_TRADES  = "sniper_trades.csv"

# --- LOCK GLOBAL ---
state_lock = threading.Lock()


# =============================================================================
# [HIBRIDO] DETECTOR DE ANOMALIAS EM TEMPO REAL (TICK-BY-TICK)
# =============================================================================
class RealtimeAnomalyDetector:
    """
    Detecta flash pumps/crashes DURANTE a vela via stream de ticks.
    Alimentado pela thread_trade_ws() que consome o @trade WebSocket da Binance.

    Latência típica: 10-15 segundos.
    Baseline: precisa de N minutos de histórico antes de operar.
    """

    def __init__(self, symbol="PAXGUSDT", baseline_minutes=60,
                 price_spike_threshold=0.003,
                 volume_spike_threshold=3.0,
                 velocity_threshold=0.001):
        self.symbol = symbol
        self.baseline_minutes = baseline_minutes

        # Thresholds (mais sensíveis que o detector de velas por ser tick-by-tick)
        self.PRICE_SPIKE_THRESHOLD  = price_spike_threshold   # 0.3%
        self.VOLUME_SPIKE_THRESHOLD = volume_spike_threshold  # 3× média
        self.VELOCITY_THRESHOLD     = velocity_threshold      # 0.1%/seg

        # Baseline: histórico de minutos fechados
        self.baseline_highs   = deque(maxlen=baseline_minutes)
        self.baseline_lows    = deque(maxlen=baseline_minutes)
        self.baseline_volumes = deque(maxlen=baseline_minutes)

        # Vela parcial do minuto atual (construída em tempo real)
        self.current_minute_start  = None
        self.current_minute_open   = None
        self.current_minute_high   = None
        self.current_minute_low    = None
        self.current_minute_close  = None
        self.current_minute_volume = 0.0
        self.current_minute_ticks  = 0

        # Buffer de ticks recentes (últimos 30s, 10 ticks/s × 30s)
        self.recent_ticks = deque(maxlen=300)

        # Para cálculo de velocidade
        self.last_price     = None
        self.last_tick_time = None

        # Última anomalia detectada (lida pelo HybridDetector)
        self.last_pump  = None   # dict ou None
        self.last_crash = None   # dict ou None
        self._last_pump_time  = None
        self._last_crash_time = None
        self.ANOMALY_TTL = 120   # segundos — anomalia expira após 2 minutos

        # Estatísticas
        self.flash_pumps_detected   = 0
        self.flash_crashes_detected = 0

        self.baseline_ready = False
        self.ENABLED        = True

    # ------------------------------------------------------------------
    def update_tick(self, price, volume, timestamp=None):
        """
        Processa um tick em tempo real.
        Chamado pela thread_trade_ws() a cada mensagem do @trade stream.
        Retorna dict de anomalia se detectada, ou None.
        """
        if not self.ENABLED:
            return None

        if timestamp is None:
            timestamp = datetime.now()

        price  = float(price)
        volume = float(volume) if volume else 0.0

        # Verifica troca de minuto
        current_minute = timestamp.replace(second=0, microsecond=0)
        if self.current_minute_start != current_minute:
            self._finalize_minute()
            self._start_new_minute(current_minute, price)

        # Atualiza vela parcial
        if self.current_minute_high is None or price > self.current_minute_high:
            self.current_minute_high = price
        if self.current_minute_low is None or price < self.current_minute_low:
            self.current_minute_low = price
        self.current_minute_close   = price
        self.current_minute_volume += volume
        self.current_minute_ticks  += 1

        self.recent_ticks.append({'price': price, 'volume': volume, 'timestamp': timestamp})

        # Velocidade de mudança de preço (% por segundo)
        velocity = 0.0
        if self.last_price and self.last_tick_time:
            time_diff = (timestamp - self.last_tick_time).total_seconds()
            if time_diff > 0:
                velocity = ((price / self.last_price) - 1) / time_diff
        self.last_price     = price
        self.last_tick_time = timestamp

        # Detecta anomalia
        if self.baseline_ready:
            anomaly = self._detect_realtime_anomaly(velocity)
            if anomaly:
                now = datetime.now()
                if anomaly['type'] == 'FLASH_PUMP':
                    self.last_pump       = anomaly
                    self._last_pump_time = now
                else:
                    self.last_crash       = anomaly
                    self._last_crash_time = now
                return anomaly

        return None

    def get_last_pump(self):
        """Retorna última anomalia de pump se ainda válida (dentro do TTL)."""
        if self.last_pump and self._last_pump_time:
            if (datetime.now() - self._last_pump_time).total_seconds() < self.ANOMALY_TTL:
                return self.last_pump
        return None

    def get_last_crash(self):
        """Retorna última anomalia de crash se ainda válida (dentro do TTL)."""
        if self.last_crash and self._last_crash_time:
            if (datetime.now() - self._last_crash_time).total_seconds() < self.ANOMALY_TTL:
                return self.last_crash
        return None

    def clear_last_pump(self):
        self.last_pump       = None
        self._last_pump_time = None

    def clear_last_crash(self):
        self.last_crash       = None
        self._last_crash_time = None

    def _start_new_minute(self, minute_start, open_price):
        self.current_minute_start  = minute_start
        self.current_minute_open   = open_price
        self.current_minute_high   = open_price
        self.current_minute_low    = open_price
        self.current_minute_close  = open_price
        self.current_minute_volume = 0.0
        self.current_minute_ticks  = 0

    def _finalize_minute(self):
        if self.current_minute_start is None:
            return
        if self.current_minute_high:
            self.baseline_highs.append(self.current_minute_high)
        if self.current_minute_low:
            self.baseline_lows.append(self.current_minute_low)
        if self.current_minute_volume > 0:
            self.baseline_volumes.append(self.current_minute_volume)
        if len(self.baseline_highs) >= self.baseline_minutes:
            self.baseline_ready = True

    def _detect_realtime_anomaly(self, velocity):
        if not self.baseline_ready or not self.baseline_highs:
            return None
        try:
            avg_high   = np.mean(self.baseline_highs)
            avg_low    = np.mean(self.baseline_lows)
            avg_volume = np.mean(self.baseline_volumes) if self.baseline_volumes else 0

            pump_spike   = ((self.current_minute_high / avg_high) - 1) if self.current_minute_high else 0
            crash_drop   = ((avg_low / self.current_minute_low) - 1)   if self.current_minute_low  else 0
            volume_ratio = self.current_minute_volume / avg_volume      if avg_volume > 0           else 0

            velocity_extreme = abs(velocity) >= self.VELOCITY_THRESHOLD

            # Flash Crash
            if crash_drop >= self.PRICE_SPIKE_THRESHOLD:
                if volume_ratio >= self.VOLUME_SPIKE_THRESHOLD or velocity_extreme:
                    confidence = min(
                        crash_drop / 0.01,
                        volume_ratio / 5.0,
                        abs(velocity) / self.VELOCITY_THRESHOLD if self.VELOCITY_THRESHOLD > 0 else 1.0,
                        1.0
                    )
                    self.flash_crashes_detected += 1
                    return {
                        "type":            "FLASH_CRASH",
                        "drop_pct":        crash_drop * 100,
                        "volume_ratio":    volume_ratio,
                        "velocity":        velocity * 100,
                        "confidence":      confidence,
                        "price_bottom":    self.current_minute_low,
                        "detection_type":  "REALTIME",
                        "elapsed_seconds": (datetime.now() - self.current_minute_start).total_seconds()
                                           if self.current_minute_start else 0,
                    }

            # Flash Pump
            if pump_spike >= self.PRICE_SPIKE_THRESHOLD:
                if volume_ratio >= self.VOLUME_SPIKE_THRESHOLD or velocity_extreme:
                    confidence = min(
                        pump_spike / 0.01,
                        volume_ratio / 5.0,
                        abs(velocity) / self.VELOCITY_THRESHOLD if self.VELOCITY_THRESHOLD > 0 else 1.0,
                        1.0
                    )
                    self.flash_pumps_detected += 1
                    return {
                        "type":            "FLASH_PUMP",
                        "spike_pct":       pump_spike * 100,
                        "volume_ratio":    volume_ratio,
                        "velocity":        velocity * 100,
                        "confidence":      confidence,
                        "price_peak":      self.current_minute_high,
                        "detection_type":  "REALTIME",
                        "elapsed_seconds": (datetime.now() - self.current_minute_start).total_seconds()
                                           if self.current_minute_start else 0,
                    }
        except Exception as e:
            logging.warning(f"[RT Detector] Erro em _detect_realtime_anomaly: {e}")
        return None

    def get_stats(self):
        return {
            "baseline_ready": self.baseline_ready,
            "baseline_size":  len(self.baseline_highs),
            "flash_pumps":    self.flash_pumps_detected,
            "flash_crashes":  self.flash_crashes_detected,
            "current_high":   self.current_minute_high,
            "current_low":    self.current_minute_low,
        }


# =============================================================================
# [HIBRIDO] DETECTOR HÍBRIDO INTELIGENTE
# =============================================================================
class HybridDetector:
    """
    Combina RealtimeAnomalyDetector (ticks) + AnomalyDetector (velas 1h)
    com scores ponderados.

    Pesos:
      • Tempo real (RT): 0.6  — rápido mas mais ruidoso
      • Velas 1h   (1h): 1.0  — lento mas confiável

    Decisões para PUMP (quando em posição com lucro ≥ 0):
      • score_rt ≥ threshold (somente RT) → SELL_PARTIAL  (50% imediato + aguarda)
      • score_1h ≥ threshold (somente 1h) → SELL_ALL
      • Ambos detectam               → SELL_ALL  (pontuação somada)
      • Partial ativo + (1h confirma OU timeout) → SELL_ALL_REMAINING

    Decisões para CRASH (quando sem posição):
      • score_rt ≥ threshold (somente RT) → BUY_EMERGENCY
      • score_1h ≥ threshold (somente 1h) → BUY_EMERGENCY
    """

    # Pesos
    WEIGHT_RT = 0.6
    WEIGHT_1H = 1.0

    def __init__(self, config, realtime_detector, candle_detector):
        self.realtime  = realtime_detector
        self.candle_1h = candle_detector

        self.MIN_CONF_SELL   = config.get("EMERGENCY_SELL_MIN_CONFIDENCE", 0.70)
        self.MIN_CONF_BUY    = config.get("EMERGENCY_BUY_MIN_CONFIDENCE",  0.80)
        self.PARTIAL_RATIO   = config.get("HYBRID_PARTIAL_SELL_RATIO",     0.50)
        self.PARTIAL_TIMEOUT = config.get("HYBRID_PARTIAL_TIMEOUT_SEC",    300)   # 5 min
        self.ENABLED         = config.get("HYBRID_ENABLED",                True)

        # Estado de venda parcial
        self.partial_sell_active = False
        self.partial_sell_time   = None
        self.partial_sell_price  = None  # preço no momento da venda parcial

        # Contadores
        self.partial_sells    = 0
        self.full_sells_rt    = 0
        self.full_sells_1h    = 0
        self.full_sells_combo = 0
        self.emergency_buys   = 0

    # ------------------------------------------------------------------
    # API PÚBLICA
    # ------------------------------------------------------------------
    def evaluate_pump(self, candle_1h_data, profit_pct, current_price):
        """
        Avalia se há flash pump e qual ação tomar.

        Retorna:
          ("SELL_ALL",          score, source_str) — venda total imediata
          ("SELL_PARTIAL",      score, source_str) — venda de 50%, aguarda confirmação
          ("SELL_ALL_REMAINING",score, source_str) — venda do restante (pós partial)
          (None, 0, "")                             — nenhuma ação
        """
        if not self.ENABLED or profit_pct < 0:
            return None, 0, ""

        rt_pump  = self.realtime.get_last_pump()
        pump_1h  = self.candle_1h.detect_flash_pump(candle_1h_data) if candle_1h_data else None

        score_rt = (rt_pump['confidence']  * self.WEIGHT_RT) if rt_pump  else 0.0
        score_1h = (pump_1h['confidence']  * self.WEIGHT_1H) if pump_1h  else 0.0

        # Se partial já está ativo, verifica condições para vender o restante
        if self.partial_sell_active:
            elapsed   = (datetime.now() - self.partial_sell_time).total_seconds()
            timeout   = elapsed >= self.PARTIAL_TIMEOUT
            confirmed = score_1h >= self.MIN_CONF_SELL

            if confirmed or timeout:
                reason = "confirmação 1h" if confirmed else f"timeout {elapsed:.0f}s"
                self.partial_sell_active = False
                return "SELL_ALL_REMAINING", max(score_rt, score_1h), f"PARTIAL→FULL ({reason})"

            return None, 0, ""   # aguarda, partial já feito

        # Sem partial ativo: avalia nova anomalia
        both_detect = rt_pump and pump_1h
        only_rt     = rt_pump  and not pump_1h
        only_1h     = pump_1h  and not rt_pump

        if both_detect:
            combined_score = min(score_rt + score_1h, 1.0)
            if combined_score >= self.MIN_CONF_SELL:
                self.full_sells_combo += 1
                self.realtime.clear_last_pump()
                return "SELL_ALL", combined_score, "RT+1h"

        if only_1h and score_1h >= self.MIN_CONF_SELL:
            self.full_sells_1h += 1
            return "SELL_ALL", score_1h, "1h"

        if only_rt and score_rt >= (self.MIN_CONF_SELL * self.WEIGHT_RT):
            # RT sozinho → venda parcial cautelosa
            self.partial_sell_active = True
            self.partial_sell_time   = datetime.now()
            self.partial_sell_price  = current_price
            self.partial_sells += 1
            self.realtime.clear_last_pump()
            return "SELL_PARTIAL", score_rt, "RT-only"

        return None, 0, ""

    def evaluate_crash(self, candle_1h_data, in_position, capital_available):
        """
        Avalia se há flash crash e se deve comprar de emergência.
        Retorna: (True, score, source) ou (False, 0, "")
        """
        if not self.ENABLED or in_position or capital_available <= 0:
            return False, 0, ""

        rt_crash = self.realtime.get_last_crash()
        crash_1h = self.candle_1h.detect_flash_crash(candle_1h_data) if candle_1h_data else None

        score_rt = (rt_crash['confidence']  * self.WEIGHT_RT) if rt_crash  else 0.0
        score_1h = (crash_1h['confidence']  * self.WEIGHT_1H) if crash_1h  else 0.0

        best_score = max(score_rt, score_1h)
        if best_score >= self.MIN_CONF_BUY:
            source = ("RT+1h" if rt_crash and crash_1h
                      else "RT"  if rt_crash
                      else "1h")
            self.emergency_buys += 1
            self.realtime.clear_last_crash()
            return True, best_score, source

        return False, 0, ""

    def reset_partial(self):
        """Reseta o estado de venda parcial (ex: após novo DCA)."""
        self.partial_sell_active = False
        self.partial_sell_time   = None
        self.partial_sell_price  = None

    def get_stats(self):
        return {
            "partial_sells":    self.partial_sells,
            "full_sells_rt":    self.full_sells_rt,
            "full_sells_1h":    self.full_sells_1h,
            "full_sells_combo": self.full_sells_combo,
            "emergency_buys":   self.emergency_buys,
            "partial_active":   self.partial_sell_active,
        }


# =============================================================================
# [M6] DETECTOR DE ANOMALIAS BASEADO EM VELAS (mantido do v5.1.0)
# =============================================================================
class AnomalyDetector:
    """
    Detecta flash pumps/crashes usando velas OHLCV fechadas (1h).
    Usado como componente do HybridDetector (peso 1.0).
    Também mantido standalone para compatibilidade com v5.1.0.
    """

    def __init__(self, config):
        self.ENABLED                = config.get("ANOMALY_DETECTOR_ENABLED",       True)
        self.PRICE_SPIKE_THRESHOLD  = config.get("PRICE_SPIKE_THRESHOLD",          0.005)
        self.VOLUME_SPIKE_THRESHOLD = config.get("VOLUME_SPIKE_THRESHOLD",         5.0)
        self.TRADES_SPIKE_THRESHOLD = config.get("TRADES_SPIKE_THRESHOLD",         10.0)
        self.LOOKBACK_CANDLES       = config.get("ANOMALY_LOOKBACK",               20)

        self.price_history  = deque(maxlen=self.LOOKBACK_CANDLES)
        self.volume_history = deque(maxlen=self.LOOKBACK_CANDLES)
        self.trades_history = deque(maxlen=self.LOOKBACK_CANDLES)

        self.flash_pumps_detected   = 0
        self.flash_crashes_detected = 0
        self.emergency_sells        = 0
        self.emergency_buys         = 0

        self.MIN_CONFIDENCE_SELL = config.get("EMERGENCY_SELL_MIN_CONFIDENCE", 0.70)
        self.MIN_CONFIDENCE_BUY  = config.get("EMERGENCY_BUY_MIN_CONFIDENCE",  0.80)

    def update(self, candle_data):
        if not self.ENABLED:
            return
        try:
            self.price_history.append(float(candle_data['c']))
            self.volume_history.append(float(candle_data['v']))
            trades = candle_data.get('trades', float(candle_data['v']) * 100)
            self.trades_history.append(float(trades))
        except Exception as e:
            logging.warning(f"Erro ao atualizar AnomalyDetector: {e}")

    def detect_flash_pump(self, current_candle):
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_spike = ((float(current_candle['h']) / float(current_candle['o'])) - 1)
            if price_spike < self.PRICE_SPIKE_THRESHOLD:
                return None
            avg_volume     = np.mean(self.volume_history)
            avg_trades     = np.mean(self.trades_history)
            current_volume = float(current_candle['v'])
            current_trades = current_candle.get('trades', current_volume * 100)
            volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 1
            trades_ratio   = current_trades / avg_trades if avg_trades > 0 else 1
            if volume_ratio < self.VOLUME_SPIKE_THRESHOLD or trades_ratio < self.TRADES_SPIKE_THRESHOLD:
                return None
            confidence = min(price_spike / 0.01, volume_ratio / 10, trades_ratio / 20, 1.0)
            self.flash_pumps_detected += 1
            return {"type": "FLASH_PUMP", "spike_pct": price_spike * 100,
                    "volume_ratio": volume_ratio, "trades_ratio": trades_ratio,
                    "confidence": confidence, "price_peak": float(current_candle['h'])}
        except Exception as e:
            logging.error(f"Erro detect_flash_pump: {e}")
            return None

    def detect_flash_crash(self, current_candle):
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_drop = ((float(current_candle['o']) / float(current_candle['l'])) - 1)
            if price_drop < self.PRICE_SPIKE_THRESHOLD:
                return None
            avg_volume     = np.mean(self.volume_history)
            avg_trades     = np.mean(self.trades_history)
            current_volume = float(current_candle['v'])
            current_trades = current_candle.get('trades', current_volume * 100)
            volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 1
            trades_ratio   = current_trades / avg_trades if avg_trades > 0 else 1
            if volume_ratio < self.VOLUME_SPIKE_THRESHOLD or trades_ratio < self.TRADES_SPIKE_THRESHOLD:
                return None
            confidence = min(price_drop / 0.01, volume_ratio / 10, trades_ratio / 20, 1.0)
            self.flash_crashes_detected += 1
            return {"type": "FLASH_CRASH", "drop_pct": price_drop * 100,
                    "volume_ratio": volume_ratio, "trades_ratio": trades_ratio,
                    "confidence": confidence, "price_bottom": float(current_candle['l'])}
        except Exception as e:
            logging.error(f"Erro detect_flash_crash: {e}")
            return None

    def should_emergency_sell(self, anomaly, in_position, profit_pct):
        if not in_position or anomaly is None or anomaly['type'] != 'FLASH_PUMP':
            return False
        if anomaly['confidence'] < self.MIN_CONFIDENCE_SELL or profit_pct < 0:
            return False
        self.emergency_sells += 1
        return True

    def should_emergency_buy(self, anomaly, in_position, capital_available):
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
        getattr(logging, {"INFO": "info", "AVISO": "warning",
                          "ERRO": "error", "CRITICO": "critical"}.get(nivel, "info"))(msg)
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
    "perda_usd_atual": 0.0,
    "proxima_compra_p": 0.0,
    "num_safety_orders": 0,
    "alvo_trailing_ativacao": 0.0,
    "stop_atual_trailing": 0.0,
    "trailing_ativo": False,
    "max_p_trailing": 0.0,
    "trailing_dist_atual": 0.0,
    "circuit_breaker": False,
    "rsi_atual": 0.0,
    # [M6] Detector velas
    "anomaly_flash_pumps": 0,
    "anomaly_flash_crashes": 0,
    "anomaly_emergency_sells": 0,
    "anomaly_emergency_buys": 0,
    # [HIBRIDO] Detector em tempo real
    "rt_baseline_ready": False,
    "rt_baseline_size": 0,
    "rt_flash_pumps": 0,
    "rt_flash_crashes": 0,
    # [HIBRIDO] Stats do HybridDetector
    "hybrid_partial_sells": 0,
    "hybrid_full_sells_rt": 0,
    "hybrid_full_sells_1h": 0,
    "hybrid_full_sells_combo": 0,
    "hybrid_emergency_buys": 0,
    "hybrid_partial_active": False,
    "hybrid_partial_elapsed": 0,
    "msg_log": "Sistema Iniciado",
}

menu_ativo        = False
exchange          = None
ordens_executadas = []

# Instâncias globais dos detectores
anomaly_detector  = None   # AnomalyDetector (velas 1h)  — protegido por state_lock
realtime_detector = None   # RealtimeAnomalyDetector     — thread-safe por deque/float
hybrid_detector   = None   # HybridDetector              — protegido por state_lock


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
    "TRAILING_TRIGGER": 0.015,
    "TRAILING_DIST":    0.005,

    # [M4] Stop Loss USD
    "STOP_LOSS_USD": 10.0,

    # [M1] Filtro RSI
    "FILTRO_RSI_ATIVO": True,           # [v5.2.1] False = RSI exibido mas não bloqueia
    "RSI_MAX_ENTRADA":  65.0,
    "RSI_PERIOD":       14,

    # [M2] Filtro de Volume
    "FILTRO_VOLUME_ATIVO": True,        # [v5.2.1] False = volume não verificado na entrada
    "VOLUME_FATOR_MIN":    1.2,

    # [M3] Circuit Breaker
    "CIRCUIT_BREAKER_ATIVO": True,      # [v5.2.1] False = quedas de 4h não pausam entradas
    "CB_QUEDA_PCT":          3.0,
    "CB_JANELA_VELAS":       5,

    # [M5] ATR para Trailing
    "ATR_PERIOD":      14,
    "ATR_MULTIPLICADOR": 1.5,

    # [M6] Detector de Anomalias (velas 1h)
    "ANOMALY_DETECTOR_ENABLED":      True,
    "PRICE_SPIKE_THRESHOLD":         0.005,
    "VOLUME_SPIKE_THRESHOLD":        5.0,
    "TRADES_SPIKE_THRESHOLD":        10.0,
    "ANOMALY_LOOKBACK":              20,
    "EMERGENCY_SELL_MIN_CONFIDENCE": 0.70,
    "EMERGENCY_BUY_MIN_CONFIDENCE":  0.80,

    # [HIBRIDO] Detector em Tempo Real + Híbrido
    "HYBRID_ENABLED":              True,
    "RT_BASELINE_MINUTES":         60,     # minutos para construir baseline RT
    "RT_PRICE_SPIKE_THRESHOLD":    0.003,  # 0.3% (mais sensível que velas)
    "RT_VOLUME_SPIKE_THRESHOLD":   3.0,    # 3× média
    "RT_VELOCITY_THRESHOLD":       0.001,  # 0.1%/segundo
    "HYBRID_PARTIAL_SELL_RATIO":   0.50,   # vende 50% em sinal RT-only
    "HYBRID_PARTIAL_TIMEOUT_SEC":  300,    # 5 min para aguardar confirmação 1h
}


# =============================================================================
# PERSISTÊNCIA
# =============================================================================
def salvar_estado_disco():
    try:
        with state_lock:
            dados = {
                "em_operacao":    shared_state["em_operacao"],
                "ordens":         list(ordens_executadas),
                "symbol":         CONFIG["SYMBOL"],
                "max_p_trailing": shared_state["max_p_trailing"],
                "trailing_ativo": shared_state["trailing_ativo"],
                "timestamp":      str(datetime.now()),
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
                ordens_executadas              = dados["ordens"]
                shared_state["em_operacao"]    = True
                shared_state["marcha"]         = "RECUPERANDO..."
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
    global exchange, CONFIG, anomaly_detector, realtime_detector, hybrid_detector

    Auditoria.configurar()
    msg_update     = ""
    mudanca_symbol = False

    if os.path.exists("config.ini"):
        cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
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
            CONFIG["STOP_LOSS_USD"]     = float(g("stop_loss_usd",     CONFIG["STOP_LOSS_USD"]))
            CONFIG["ATR_PERIOD"]        = int(  g("atr_period",        CONFIG["ATR_PERIOD"]))
            CONFIG["ATR_MULTIPLICADOR"] = float(g("atr_multiplicador", CONFIG["ATR_MULTIPLICADOR"]))

            # [M1] Filtro RSI — flag + parâmetros
            CONFIG["FILTRO_RSI_ATIVO"]  = cp["trading"].getboolean("filtro_rsi_ativo",  CONFIG["FILTRO_RSI_ATIVO"])
            CONFIG["RSI_MAX_ENTRADA"]   = float(g("rsi_max_entrada",   CONFIG["RSI_MAX_ENTRADA"]))
            CONFIG["RSI_PERIOD"]        = int(  g("rsi_period",        CONFIG["RSI_PERIOD"]))

            # [M2] Filtro de Volume — flag + parâmetro
            CONFIG["FILTRO_VOLUME_ATIVO"] = cp["trading"].getboolean("filtro_volume_ativo", CONFIG["FILTRO_VOLUME_ATIVO"])
            CONFIG["VOLUME_FATOR_MIN"]    = float(g("volume_fator_min", CONFIG["VOLUME_FATOR_MIN"]))

            # [M3] Circuit Breaker — flag + parâmetros
            CONFIG["CIRCUIT_BREAKER_ATIVO"] = cp["trading"].getboolean("circuit_breaker_ativo", CONFIG["CIRCUIT_BREAKER_ATIVO"])
            CONFIG["CB_QUEDA_PCT"]          = float(g("cb_queda_pct",    CONFIG["CB_QUEDA_PCT"]))
            CONFIG["CB_JANELA_VELAS"]       = int(  g("cb_janela_velas", CONFIG["CB_JANELA_VELAS"]))

            # [M6] Detector de velas
            if "anomaly" in cp:
                ga = cp["anomaly"].get
                CONFIG["ANOMALY_DETECTOR_ENABLED"]      = cp["anomaly"].getboolean("enabled", CONFIG["ANOMALY_DETECTOR_ENABLED"])
                CONFIG["PRICE_SPIKE_THRESHOLD"]         = float(ga("price_spike_threshold",         CONFIG["PRICE_SPIKE_THRESHOLD"]))
                CONFIG["VOLUME_SPIKE_THRESHOLD"]        = float(ga("volume_spike_threshold",        CONFIG["VOLUME_SPIKE_THRESHOLD"]))
                CONFIG["TRADES_SPIKE_THRESHOLD"]        = float(ga("trades_spike_threshold",        CONFIG["TRADES_SPIKE_THRESHOLD"]))
                CONFIG["ANOMALY_LOOKBACK"]              = int(  ga("lookback_candles",              CONFIG["ANOMALY_LOOKBACK"]))
                CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"] = float(ga("emergency_sell_min_confidence", CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"]))
                CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]  = float(ga("emergency_buy_min_confidence",  CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]))

            # [HIBRIDO] Configuração do híbrido
            if "hybrid" in cp:
                gh = cp["hybrid"].get
                CONFIG["HYBRID_ENABLED"]             = cp["hybrid"].getboolean("enabled",                CONFIG["HYBRID_ENABLED"])
                CONFIG["RT_BASELINE_MINUTES"]        = int(  gh("rt_baseline_minutes",        CONFIG["RT_BASELINE_MINUTES"]))
                CONFIG["RT_PRICE_SPIKE_THRESHOLD"]   = float(gh("rt_price_spike_threshold",   CONFIG["RT_PRICE_SPIKE_THRESHOLD"]))
                CONFIG["RT_VOLUME_SPIKE_THRESHOLD"]  = float(gh("rt_volume_spike_threshold",  CONFIG["RT_VOLUME_SPIKE_THRESHOLD"]))
                CONFIG["RT_VELOCITY_THRESHOLD"]      = float(gh("rt_velocity_threshold",      CONFIG["RT_VELOCITY_THRESHOLD"]))
                CONFIG["HYBRID_PARTIAL_SELL_RATIO"]  = float(gh("partial_sell_ratio",         CONFIG["HYBRID_PARTIAL_SELL_RATIO"]))
                CONFIG["HYBRID_PARTIAL_TIMEOUT_SEC"] = int(  gh("partial_sell_timeout_sec",   CONFIG["HYBRID_PARTIAL_TIMEOUT_SEC"]))

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

    # [M6] Inicializa detector de velas
    anomaly_detector = AnomalyDetector(CONFIG)
    Auditoria.log_sistema(
        f"Detector velas: {'ATIVO' if CONFIG['ANOMALY_DETECTOR_ENABLED'] else 'DESATIVADO'}", "INFO")

    # [HIBRIDO] Inicializa detector em tempo real
    symbol_raw = CONFIG["SYMBOL"].replace("/", "")
    realtime_detector = RealtimeAnomalyDetector(
        symbol                 = symbol_raw,
        baseline_minutes       = CONFIG["RT_BASELINE_MINUTES"],
        price_spike_threshold  = CONFIG["RT_PRICE_SPIKE_THRESHOLD"],
        volume_spike_threshold = CONFIG["RT_VOLUME_SPIKE_THRESHOLD"],
        velocity_threshold     = CONFIG["RT_VELOCITY_THRESHOLD"],
    )
    realtime_detector.ENABLED = CONFIG["HYBRID_ENABLED"]

    # [HIBRIDO] Inicializa detector híbrido
    hybrid_detector = HybridDetector(CONFIG, realtime_detector, anomaly_detector)

    # [v5.2.1] Log do estado das flags de filtro
    Auditoria.log_sistema(
        f"Filtros: RSI={'ATIVO' if CONFIG['FILTRO_RSI_ATIVO'] else 'DESATIVADO'} "
        f"| Volume={'ATIVO' if CONFIG['FILTRO_VOLUME_ATIVO'] else 'DESATIVADO'} "
        f"| CircuitBreaker={'ATIVO' if CONFIG['CIRCUIT_BREAKER_ATIVO'] else 'DESATIVADO'}", "INFO")

    Auditoria.log_sistema(
        f"Detector híbrido: {'ATIVO' if CONFIG['HYBRID_ENABLED'] else 'DESATIVADO'} "
        f"| RT baseline: {CONFIG['RT_BASELINE_MINUTES']} min "
        f"| Partial ratio: {CONFIG['HYBRID_PARTIAL_SELL_RATIO']*100:.0f}% "
        f"| Timeout: {CONFIG['HYBRID_PARTIAL_TIMEOUT_SEC']}s", "INFO")


# =============================================================================
# UTILITÁRIOS
# =============================================================================
def definir_status(msg, tipo="INFO"):
    hora = datetime.now().strftime("%H:%M:%S")
    cor  = {"SUCESSO": Fore.GREEN, "ERRO": Fore.RED, "AVISO": Fore.YELLOW}.get(tipo, Fore.CYAN)
    with state_lock:
        shared_state["msg_log"] = f"{Fore.WHITE}[{hora}] {cor}{msg}"
    if tipo == "ERRO":      Auditoria.log_sistema(msg, "ERRO")
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
    delta = df['c'].diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, float('nan'))
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calcular_atr(df: pd.DataFrame, period: int) -> float:
    high_low   = df['h'] - df['l']
    high_close = (df['h'] - df['c'].shift()).abs()
    low_close  = (df['l'] - df['c'].shift()).abs()
    tr  = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
    preco_atual = df['c'].iloc[-1]
    return float(atr / preco_atual) if preco_atual > 0 else CONFIG["TRAILING_DIST"]


def trailing_dist_por_atr(df_1h: pd.DataFrame) -> float:
    try:
        atr_perc = calcular_atr(df_1h, CONFIG["ATR_PERIOD"])
        dist     = atr_perc * CONFIG["ATR_MULTIPLICADOR"]
        return max(dist, CONFIG["TRAILING_DIST"])
    except Exception:
        return CONFIG["TRAILING_DIST"]


# =============================================================================
# ANÁLISE MTF + FILTROS
# =============================================================================
def check_mtf_trend() -> bool:
    config_tf       = {'15m': 25, '1h': 50, '4h': 100}
    filtros         = {}
    votos_positivos = 0
    try:
        for tf, period in config_tf.items():
            ohlcv = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe=tf, limit=period + 5)
            df    = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
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
    # [v5.2.1] Se circuit breaker desativado, força estado False e retorna sem chamar API
    if not CONFIG["CIRCUIT_BREAKER_ATIVO"]:
        with state_lock:
            shared_state["circuit_breaker"] = False
        return False

    try:
        janela = CONFIG["CB_JANELA_VELAS"] + 2
        ohlcv  = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe='4h', limit=janela)
        df     = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        preco_inicio = float(df['c'].iloc[-(CONFIG["CB_JANELA_VELAS"] + 1)])
        preco_atual  = float(df['c'].iloc[-1])
        variacao_pct = ((preco_atual / preco_inicio) - 1) * 100
        ativado      = variacao_pct <= -CONFIG["CB_QUEDA_PCT"]
        with state_lock:
            shared_state["circuit_breaker"] = ativado
        if ativado:
            definir_status(
                f"⚡ Circuit Breaker: mercado caiu {variacao_pct:.2f}% em "
                f"{CONFIG['CB_JANELA_VELAS']} velas de 4h. Entradas pausadas.", "AVISO")
        return ativado
    except Exception as e:
        Auditoria.log_sistema(f"Erro Circuit Breaker: {e}", "ERRO")
        return False


def verificar_filtros_entrada(df_1h: pd.DataFrame) -> tuple[bool, str]:
    # --- RSI [M1] ---
    # Sempre calcula para manter o dashboard atualizado.
    # Só bloqueia entrada se FILTRO_RSI_ATIVO = True.
    try:
        rsi = calcular_rsi(df_1h, CONFIG["RSI_PERIOD"])
        with state_lock:
            shared_state["rsi_atual"] = rsi
        if CONFIG["FILTRO_RSI_ATIVO"] and rsi >= CONFIG["RSI_MAX_ENTRADA"]:
            return False, f"RSI sobrecomprado ({rsi:.1f} >= {CONFIG['RSI_MAX_ENTRADA']})"
    except Exception as e:
        Auditoria.log_sistema(f"Erro filtro RSI: {e}", "AVISO")

    # --- Volume [M2] ---
    if CONFIG["FILTRO_VOLUME_ATIVO"]:
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
    try:
        ohlcv = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe='1h', limit=limit)
        return pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
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
        preco_exec  = float(ordem.get('average') or ordem.get('price') or 0)
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
        shared_state["em_operacao"]            = False
        shared_state["trailing_ativo"]         = False
        shared_state["max_p_trailing"]         = 0.0
        shared_state["alvo_trailing_ativacao"] = 0.0
        shared_state["stop_atual_trailing"]    = 0.0
        shared_state["perda_usd_atual"]        = 0.0
        shared_state["trailing_dist_atual"]    = 0.0
        shared_state["hybrid_partial_active"]  = False
        shared_state["hybrid_partial_elapsed"] = 0
    if hybrid_detector:
        hybrid_detector.reset_partial()
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

    trailing_dist = CONFIG["TRAILING_DIST"]
    df_1h_inicial = obter_df_1h()
    if df_1h_inicial is not None:
        trailing_dist = trailing_dist_por_atr(df_1h_inicial)
    with state_lock:
        shared_state["trailing_dist_atual"] = trailing_dist

    # [HIBRIDO] Estado de venda parcial local (sincronizado com hybrid_detector)
    partial_sell_qtd_restante   = 0.0
    partial_sell_custo_restante = 0.0
    partial_sell_ativa          = False

    while True:
        with state_lock:
            erros = shared_state.get("erros_consecutivos", 0)
        if erros >= 5:
            panico_sistema("Múltiplos erros consecutivos detectados.")
            break

        try:
            # ------------------------------------------------------------------
            # Atualiza detector de velas + estado RT no dashboard
            # ------------------------------------------------------------------
            df_1h_atual  = obter_df_1h(limit=25)
            candle_atual = None

            if df_1h_atual is not None and len(df_1h_atual) > 0:
                candle_atual = {
                    'o': df_1h_atual['o'].iloc[-1],
                    'h': df_1h_atual['h'].iloc[-1],
                    'l': df_1h_atual['l'].iloc[-1],
                    'c': df_1h_atual['c'].iloc[-1],
                    'v': df_1h_atual['v'].iloc[-1],
                }
                anomaly_detector.update(candle_atual)

            # Sincroniza stats no dashboard
            with state_lock:
                shared_state["anomaly_flash_pumps"]    = anomaly_detector.flash_pumps_detected
                shared_state["anomaly_flash_crashes"]  = anomaly_detector.flash_crashes_detected
                shared_state["anomaly_emergency_sells"] = anomaly_detector.emergency_sells
                shared_state["anomaly_emergency_buys"]  = anomaly_detector.emergency_buys

                if realtime_detector:
                    rt_stats = realtime_detector.get_stats()
                    shared_state["rt_baseline_ready"] = rt_stats["baseline_ready"]
                    shared_state["rt_baseline_size"]  = rt_stats["baseline_size"]
                    shared_state["rt_flash_pumps"]    = rt_stats["flash_pumps"]
                    shared_state["rt_flash_crashes"]  = rt_stats["flash_crashes"]

                if hybrid_detector:
                    hstats = hybrid_detector.get_stats()
                    shared_state["hybrid_partial_sells"]    = hstats["partial_sells"]
                    shared_state["hybrid_full_sells_rt"]    = hstats["full_sells_rt"]
                    shared_state["hybrid_full_sells_1h"]    = hstats["full_sells_1h"]
                    shared_state["hybrid_full_sells_combo"] = hstats["full_sells_combo"]
                    shared_state["hybrid_emergency_buys"]   = hstats["emergency_buys"]
                    shared_state["hybrid_partial_active"]   = hstats["partial_active"]
                    if hstats["partial_active"] and hybrid_detector.partial_sell_time:
                        elapsed = (datetime.now() - hybrid_detector.partial_sell_time).total_seconds()
                        shared_state["hybrid_partial_elapsed"] = int(elapsed)

            with state_lock:
                em_op = shared_state["em_operacao"]
                preco = shared_state["preco"]

            if not em_op:
                break
            if preco <= 0:
                time.sleep(0.5)
                continue

            pm               = calcular_preco_medio()
            lucro_atual_perc = ((preco / pm) - 1) * 100

            with state_lock:
                n_ordens          = len(ordens_executadas)
                custo_total_ciclo = sum(o['custo']  for o in ordens_executadas)
                qtd_total_ciclo   = sum(o['volume'] for o in ordens_executadas)

            if partial_sell_ativa:
                qtd_efetiva   = partial_sell_qtd_restante
                custo_efetivo = partial_sell_custo_restante
            else:
                qtd_efetiva   = qtd_total_ciclo
                custo_efetivo = custo_total_ciclo

            valor_atual_ciclo = preco * qtd_efetiva
            perda_usd         = custo_efetivo - valor_atual_ciclo

            with state_lock:
                shared_state["preco_medio"]       = pm
                shared_state["lucro_perc_atual"]  = lucro_atual_perc
                shared_state["perda_usd_atual"]   = perda_usd
                shared_state["num_safety_orders"] = n_ordens - 1
                shared_state["proxima_compra_p"]  = prox_p if prox_p else 0.0
                shared_state["max_p_trailing"]    = max_p

            # ------------------------------------------------------------------
            # 1. GESTÃO DE COMPRA (DCA)
            # ------------------------------------------------------------------
            if not partial_sell_ativa and prox_p and preco <= prox_p:
                msg_ordem = f"DCA #{n_ordens}"
                definir_status(f"Executando {msg_ordem}...", "AVISO")
                res = comprar_com_vault(prox_v_usd, motivo=msg_ordem)
                if res['ok']:
                    with state_lock:
                        ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                        n_ordens = len(ordens_executadas)
                    trailing_ativo = False
                    max_p          = 0.0
                    hybrid_detector.reset_partial()

                    df_1h_novo = obter_df_1h()
                    if df_1h_novo is not None:
                        trailing_dist = trailing_dist_por_atr(df_1h_novo)
                        with state_lock:
                            shared_state["trailing_dist_atual"] = trailing_dist

                    salvar_estado_disco()
                    prox_p, prox_v_usd = calcular_proxima_safety_order(res['p'], n_ordens - 1)
                else:
                    definir_status(f"Falha DCA: {res.get('msg', 'Erro desconhecido')}", "ERRO")

            # ------------------------------------------------------------------
            # 2. [M4] STOP LOSS DINÂMICO EM USD
            # ------------------------------------------------------------------
            if perda_usd >= CONFIG["STOP_LOSS_USD"]:
                definir_status(
                    f"🛑 STOP LOSS USD: prejuízo ${perda_usd:.2f} >= "
                    f"${CONFIG['STOP_LOSS_USD']:.2f}", "ERRO")
                res_venda = executar_venda_mercado(qtd_efetiva, motivo="SL USD")
                if res_venda['ok']:
                    Auditoria.log_transacao(
                        "STOP LOSS", res_venda['p'], qtd_efetiva, res_venda['c'],
                        lucro_usd=-perda_usd,
                        lucro_perc=lucro_atual_perc,
                        obs=f"SL USD (limite ${CONFIG['STOP_LOSS_USD']:.2f})"
                             + (" [pós-partial]" if partial_sell_ativa else ""))
                    finalizar_ciclo(motivo="STOP LOSS USD")
                    break

            # ------------------------------------------------------------------
            # 2.5. [HIBRIDO] DETECTOR HÍBRIDO (FLASH PUMP)
            # ------------------------------------------------------------------
            if hybrid_detector and hybrid_detector.ENABLED and candle_atual is not None:

                action, score, source = hybrid_detector.evaluate_pump(
                    candle_atual, lucro_atual_perc, preco)

                if action == "SELL_ALL":
                    definir_status(
                        f"🚨 HYBRID SELL_ALL! [{source}] Score: {score:.0%} | "
                        f"Lucro: {lucro_atual_perc:+.2f}%", "ERRO")
                    res_venda = executar_venda_mercado(qtd_efetiva, motivo=f"HYBRID PUMP [{source}]")
                    if res_venda['ok']:
                        Auditoria.log_transacao(
                            tipo="EMERGÊNCIA PUMP HYBRID",
                            preco=res_venda['p'],
                            qtd=qtd_efetiva,
                            total_usd=res_venda['c'],
                            lucro_usd=(res_venda['c'] - custo_efetivo),
                            lucro_perc=lucro_atual_perc,
                            saldo_vault=get_saldo_fundos(),
                            obs=f"Hybrid [{source}] Score {score:.0%}")
                        finalizar_ciclo(motivo=f"HYBRID PUMP [{source}]")
                        break

                elif action == "SELL_PARTIAL" and not partial_sell_ativa:
                    qtd_vender = qtd_total_ciclo * CONFIG["HYBRID_PARTIAL_SELL_RATIO"]
                    custo_frac = custo_total_ciclo * CONFIG["HYBRID_PARTIAL_SELL_RATIO"]
                    definir_status(
                        f"⚡ HYBRID PARTIAL SELL ({CONFIG['HYBRID_PARTIAL_SELL_RATIO']*100:.0f}%) "
                        f"[{source}] Score: {score:.0%} | Aguardando confirmação 1h...", "AVISO")
                    res_venda = executar_venda_mercado(qtd_vender, motivo=f"HYBRID PARTIAL [{source}]")
                    if res_venda['ok']:
                        partial_sell_ativa          = True
                        partial_sell_qtd_restante   = qtd_total_ciclo - qtd_vender
                        partial_sell_custo_restante = custo_total_ciclo - custo_frac
                        Auditoria.log_transacao(
                            tipo="PARTIAL SELL HYBRID",
                            preco=res_venda['p'],
                            qtd=qtd_vender,
                            total_usd=res_venda['c'],
                            lucro_usd=(res_venda['c'] - custo_frac),
                            lucro_perc=lucro_atual_perc,
                            saldo_vault=get_saldo_fundos(),
                            obs=f"Hybrid RT-only [{source}] Score {score:.0%} "
                                f"| Restante: {partial_sell_qtd_restante:.8f} unid")

                elif action == "SELL_ALL_REMAINING" and partial_sell_ativa:
                    definir_status(
                        f"✅ HYBRID VENDA RESTANTE! [{source}] Score: {score:.0%}", "SUCESSO")
                    res_venda = executar_venda_mercado(
                        partial_sell_qtd_restante, motivo=f"HYBRID REMAINING [{source}]")
                    if res_venda['ok']:
                        Auditoria.log_transacao(
                            tipo="SELL REMAINING HYBRID",
                            preco=res_venda['p'],
                            qtd=partial_sell_qtd_restante,
                            total_usd=res_venda['c'],
                            lucro_usd=(res_venda['c'] - partial_sell_custo_restante),
                            lucro_perc=lucro_atual_perc,
                            saldo_vault=get_saldo_fundos(),
                            obs=f"Hybrid confirmação [{source}] Score {score:.0%}")
                        finalizar_ciclo(motivo=f"HYBRID PUMP FULL [{source}]")
                        break

            # ------------------------------------------------------------------
            # 3. [M5] TRAILING STOP COM ATR
            # ------------------------------------------------------------------
            if not partial_sell_ativa:
                gatilho_ts = pm * (1 + CONFIG["TRAILING_TRIGGER"])
                with state_lock:
                    shared_state["alvo_trailing_ativacao"] = gatilho_ts

                if not trailing_ativo:
                    if preco >= gatilho_ts:
                        trailing_ativo = True
                        max_p          = preco
                        with state_lock:
                            shared_state["trailing_ativo"] = True
                            shared_state["max_p_trailing"] = max_p
                        definir_status(
                            f"✅ Trailing Ativado! Dist ATR: {trailing_dist * 100:.2f}%", "SUCESSO")
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
                        res_venda = executar_venda_mercado(qtd_efetiva, motivo="TP Trailing")
                        if res_venda['ok']:
                            Auditoria.log_transacao(
                                tipo="TAKE PROFIT",
                                preco=res_venda['p'],
                                qtd=qtd_efetiva,
                                total_usd=res_venda['c'],
                                lucro_usd=(res_venda['c'] - custo_efetivo),
                                lucro_perc=lucro_atual_perc,
                                saldo_vault=get_saldo_fundos(),
                                obs=f"Trailing ATR {trailing_dist * 100:.2f}%")
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
    while True:
        try:
            cb_ativo   = verificar_circuit_breaker()
            sinal_bool = check_mtf_trend() if not cb_ativo else False
            with state_lock:
                shared_state["mtf"] = "COMPRA" if sinal_bool else "AGUARDANDO"

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
                em_op     = shared_state["em_operacao"]
                sinal_mtf = shared_state["mtf"]
                preco     = shared_state["preco"]
                cb_ativo  = shared_state["circuit_breaker"]

            if not em_op:
                with state_lock:
                    shared_state["marcha"]          = "ESPERANDO MTF"
                    shared_state["preco_medio"]      = 0
                    shared_state["proxima_compra_p"] = 0

                if cb_ativo:
                    time.sleep(1)
                    continue

                # [HIBRIDO] Verifica compra de emergência por flash crash
                if hybrid_detector and hybrid_detector.ENABLED and preco > 0:
                    df_1h_crash  = obter_df_1h(limit=25)
                    candle_crash = None
                    if df_1h_crash is not None and len(df_1h_crash) > 0:
                        candle_crash = {
                            'o': df_1h_crash['o'].iloc[-1],
                            'h': df_1h_crash['h'].iloc[-1],
                            'l': df_1h_crash['l'].iloc[-1],
                            'c': df_1h_crash['c'].iloc[-1],
                            'v': df_1h_crash['v'].iloc[-1],
                        }
                    saldo_vault = get_saldo_fundos()
                    should_buy, score_crash, source_crash = hybrid_detector.evaluate_crash(
                        candle_crash, em_op, saldo_vault)

                    if should_buy:
                        definir_status(
                            f"🟢 HYBRID CRASH BUY! [{source_crash}] Score: {score_crash:.0%} "
                            f"| Comprando no fundo...", "SUCESSO")
                        res = comprar_com_vault(CONFIG["CAPITAL_BASE"],
                                               motivo=f"EMERGÊNCIA CRASH HYBRID [{source_crash}]")
                        if res['ok']:
                            Auditoria.log_transacao(
                                tipo="EMERGÊNCIA CRASH HYBRID",
                                preco=res['p'],
                                qtd=res['v'],
                                total_usd=res['c'],
                                obs=f"Flash Crash [{source_crash}] Score {score_crash:.0%}")
                            with state_lock:
                                ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                            salvar_estado_disco()
                            loop_dca_ativo()
                            continue

                if sinal_mtf == "COMPRA" and preco > 0:
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
# [HIBRIDO] THREAD TRADE WEBSOCKET (@trade stream — alimenta RealtimeAnomalyDetector)
# =============================================================================
async def trade_ws_loop():
    """
    Consome o @trade stream da Binance tick-a-tick.
    Alimenta o realtime_detector com cada negociação executada.
    Roda em loop com reconexão automática.
    """
    ultimo_symbol = CONFIG['SYMBOL'].replace('/', '').lower()
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol}@trade"

    while True:
        try:
            symbol_atual = CONFIG['SYMBOL'].replace('/', '').lower()
            if symbol_atual != ultimo_symbol:
                ultimo_symbol = symbol_atual
                uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol}@trade"
                Auditoria.log_sistema(f"Trade WS: novo símbolo {ultimo_symbol}", "INFO")

            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                Auditoria.log_sistema(f"Trade WS conectado: {ultimo_symbol}@trade", "INFO")
                with state_lock:
                    shared_state["msg_log"] = (
                        f"{Fore.WHITE}[{datetime.now():%H:%M:%S}] "
                        f"{Fore.GREEN}Trade WS conectado — RT detector alimentado")

                async for message in ws:
                    if CONFIG['SYMBOL'].replace('/', '').lower() != ultimo_symbol:
                        break

                    data     = json.loads(message)
                    price    = float(data['p'])
                    quantity = float(data['q'])
                    ts       = datetime.fromtimestamp(data['T'] / 1000)

                    if realtime_detector and realtime_detector.ENABLED:
                        anomaly = realtime_detector.update_tick(price, quantity, ts)
                        if anomaly:
                            tipo = anomaly['type']
                            if tipo == 'FLASH_PUMP':
                                Auditoria.log_sistema(
                                    f"[RT] FLASH PUMP detectado! "
                                    f"Spike: {anomaly['spike_pct']:.2f}% | "
                                    f"Vol: {anomaly['volume_ratio']:.1f}× | "
                                    f"Vel: {anomaly['velocity']:.3f}%/s | "
                                    f"Conf: {anomaly['confidence']:.0%} | "
                                    f"Detectado em {anomaly['elapsed_seconds']:.1f}s", "AVISO")
                            elif tipo == 'FLASH_CRASH':
                                Auditoria.log_sistema(
                                    f"[RT] FLASH CRASH detectado! "
                                    f"Queda: {anomaly['drop_pct']:.2f}% | "
                                    f"Vol: {anomaly['volume_ratio']:.1f}× | "
                                    f"Vel: {anomaly['velocity']:.3f}%/s | "
                                    f"Conf: {anomaly['confidence']:.0%} | "
                                    f"Detectado em {anomaly['elapsed_seconds']:.1f}s", "AVISO")

        except Exception as e:
            logging.warning(f"Trade WS desconectado: {e}. Reconectando em 3s...")
            await asyncio.sleep(3)


def iniciar_trade_ws():
    """Roda o trade_ws_loop em um event loop dedicado."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trade_ws_loop())


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
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v5.2.1 {Fore.WHITE}| DETECTOR HÍBRIDO | {Fore.CYAN}UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*80}")

            if s["circuit_breaker"]:
                print(f"{Fore.RED}  ⚡ CIRCUIT BREAKER ATIVO — Entradas pausadas por queda de mercado")

            filtros = " ".join([f"[{k}:{v}{Fore.WHITE}]" for k, v in s["filtros"].items()])
            rsi_cor = Fore.RED if s["rsi_atual"] >= CONFIG["RSI_MAX_ENTRADA"] else Fore.GREEN
            print(f"  MERCADO: {Fore.GREEN}{cfg_symbol} {Fore.YELLOW}${c:.8f} "
                  f"{Fore.WHITE}| MTF {filtros} | RSI {rsi_cor}{s['rsi_atual']:.1f}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")

            # [v5.2.1] Linha de status das flags de filtro
            rsi_tag = f"{Fore.GREEN}ON" if CONFIG["FILTRO_RSI_ATIVO"]      else f"{Fore.RED}OFF"
            vol_tag = f"{Fore.GREEN}ON" if CONFIG["FILTRO_VOLUME_ATIVO"]   else f"{Fore.RED}OFF"
            cb_tag  = f"{Fore.GREEN}ON" if CONFIG["CIRCUIT_BREAKER_ATIVO"] else f"{Fore.RED}OFF"
            print(f"  FILTROS: RSI[{rsi_tag}{Fore.WHITE}] "
                  f"VOL[{vol_tag}{Fore.WHITE}] "
                  f"CB[{cb_tag}{Fore.WHITE}]")

            # Linha de status do Detector Híbrido
            rt_ready  = s["rt_baseline_ready"]
            rt_color  = Fore.GREEN if rt_ready else Fore.YELLOW
            rt_ok_str = "OK" if rt_ready else f"aguard.baseline ({s['rt_baseline_size']}/{CONFIG['RT_BASELINE_MINUTES']})"
            rt_label  = f"RT {rt_ok_str}"

            hybrid_label = ""
            if s["hybrid_partial_active"]:
                timeout_total = CONFIG["HYBRID_PARTIAL_TIMEOUT_SEC"]
                elapsed       = s["hybrid_partial_elapsed"]
                pct_timeout   = min(elapsed / timeout_total, 1.0)
                blocos        = int(pct_timeout * 10)
                barra         = "█" * blocos + "░" * (10 - blocos)
                hybrid_label  = (f" {Fore.YELLOW}⏳ PARTIAL ATIVO "
                                 f"[{barra}] {elapsed}s/{timeout_total}s")

            print(f"  HÍBRIDO: {rt_color}{rt_label}{Fore.WHITE} | "
                  f"RT pumps:{s['rt_flash_pumps']} crashes:{s['rt_flash_crashes']} | "
                  f"Partial:{s['hybrid_partial_sells']} "
                  f"Full-RT:{s['hybrid_full_sells_rt']} "
                  f"Full-1h:{s['hybrid_full_sells_1h']} "
                  f"Combo:{s['hybrid_full_sells_combo']}"
                  f"{hybrid_label}")

            if s["em_operacao"]:
                print(f"{Fore.YELLOW}{'-'*80}")
                pnl     = s['lucro_perc_atual']
                perda   = s['perda_usd_atual']
                cor_pnl = Fore.GREEN if pnl > 0 else Fore.RED
                partial_tag = f" {Fore.YELLOW}[PARTIAL ATIVO]" if s["hybrid_partial_active"] else ""
                print(f" P. MÉDIO: {Fore.WHITE}${s['preco_medio']:.8f} "
                      f"{cor_pnl}{pnl:+.2f}%  (${perda:.2f} USD em risco / limite ${cfg_sl_usd:.2f})"
                      f"{partial_tag}")

                if cfg_sl_usd > 0:
                    progresso_sl  = min(perda / cfg_sl_usd, 1.0)
                    blocos_cheios = int(progresso_sl * 20)
                    barra_sl      = "█" * blocos_cheios + "░" * (20 - blocos_cheios)
                    cor_sl        = (Fore.GREEN  if progresso_sl < 0.5
                                     else Fore.YELLOW if progresso_sl < 0.8
                                     else Fore.RED)
                    print(f"   SL USD: {cor_sl}[{barra_sl}] {progresso_sl*100:.0f}%")

                usadas = s['num_safety_orders']
                barra  = "▰" * usadas + "▱" * (cfg_max_so - usadas)
                print(f"   SAFETY: {Fore.YELLOW}{barra} ({usadas}/{cfg_max_so})")

                if s['proxima_compra_p'] > 0 and not s["hybrid_partial_active"]:
                    dist_dca = ((s['proxima_compra_p'] / c) - 1) * 100 if c > 0 else 0
                    print(f" DCA PROX: {Fore.RED}${s['proxima_compra_p']:.8f} ({dist_dca:.2f}%)")
                elif s["hybrid_partial_active"]:
                    print(f" DCA PROX: {Fore.YELLOW}Bloqueado (aguardando confirmação hybrid)")

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
    print(f"{Fore.MAGENTA}║    MENU DE CONTROLE SNIPER v5.2      ║")
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
# WEBSOCKET DE PREÇO (@ticker — mantido do v5.1.0)
# =============================================================================
async def ws_loop():
    ultimo_symbol = CONFIG['SYMBOL']
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"

    while True:
        try:
            if ultimo_symbol != CONFIG['SYMBOL']:
                ultimo_symbol = CONFIG['SYMBOL']
                uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"
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

    # Threads originais
    threading.Thread(target=thread_motor,   daemon=True, name="Motor").start()
    threading.Thread(target=thread_visual,  daemon=True, name="Visual").start()
    threading.Thread(target=iniciar_ws,     daemon=True, name="WebSocket-Ticker").start()
    threading.Thread(target=thread_scanner, daemon=True, name="Scanner").start()

    # [HIBRIDO] Thread do @trade stream para o RealtimeAnomalyDetector
    threading.Thread(target=iniciar_trade_ws, daemon=True, name="WebSocket-Trade").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)
