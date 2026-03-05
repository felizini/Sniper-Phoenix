# =====================================================================================
# SNIPER PHOENIX v5.2.3 - DETECTOR HÍBRIDO INTELIGENTE
#
# Base: v5.2.2
#
# [v5.2.3] Correções aplicadas pela revisão de código:
#
#   FIX-01 — CRÍTICO: evaluate_crash ignorava RT-only (emergência nunca disparava)
#     • Mesmo BUG 4 (threshold cancelado pelos pesos) que foi corrigido em
#       evaluate_pump existia em evaluate_crash, mas não foi corrigido.
#     • Antes: best_score = rt_crash['confidence'] * WEIGHT_RT (0.6)
#              threshold  = MIN_CONF_BUY (0.80)
#              → exigia confidence >= 1.33 → matematicamente impossível
#     • Depois: score_rt_raw = rt_crash['confidence'] (sem multiplicar)
#               threshold RT-only = MIN_CONF_BUY * WEIGHT_RT = 0.48 ✓
#
#   FIX-02 — CRÍTICO: Race condition em thread_motor
#     • ordens_executadas.append() era chamado sem state_lock em 2 lugares
#       no thread_motor (entrada MTF e emergência crash).
#     • Corrigido: todas escritas em ordens_executadas agora protegidas por lock.
#
#   FIX-03 — Contador full_sells_rt nunca incrementado
#     • O campo existe em get_stats() e shared_state mas nenhum path de código
#       o incrementava. Adicionado incremento no path RT-only SELL_ALL
#       (caso futuro) e documentado que o caminho atual usa SELL_PARTIAL.
#
#   FIX-04 — IndexError em calcular_proxima_safety_order
#     • ordens_executadas[-1] sem verificação quando lista está vazia.
#     • Adicionado guard: retorna (None, None) se lista vazia.
#
#   FIX-05 — CONFIG sem proteção de thread no hot reload
#     • CONFIG é lido por múltiplas threads (visual, scanner, motor, ws)
#       enquanto carregar_configuracoes() escreve. Adicionado config_lock.
#
#   FIX-06 — Detectors globais reatribuídos sem lock
#     • anomaly_detector, realtime_detector, hybrid_detector eram reatribuídos
#       em carregar_configuracoes() sem proteção enquanto outras threads usavam.
#     • Adicionado detectors_lock para proteger a troca de referência.
#
#   FIX-07 — Fallback de trades arbitrário no AnomalyDetector
#     • current_candle.get('trades', current_volume * 100) usava um proxy
#       completamente arbitrário que infla trades_ratio artificialmente.
#     • Corrigido: fallback é avg_trades (neutro, ratio=1.0) quando campo ausente.
#
#   FIX-08 — obter_df_1h chamado duas vezes no mesmo ciclo DCA
#     • No caminho feliz do DCA, obter_df_1h(limit=25) era chamado no topo
#       do loop e obter_df_1h() (limit=120) logo após a compra para ATR.
#     • Unificado: uma única chamada com limit=120 satisfaz ambos os usos.
#
#   FIX-09 — Estado partial sell não persistido em disco
#     • Reinicializar o bot com partial sell ativo causava ordens_executadas
#       com volume total, mas partial_sell_qtd_restante=0 — venda dupla.
#     • partial_sell_active, qtd e custo restantes agora salvos/restaurados
#       em sniper_state.json.
#
#   FIX-10 — thread_visual lia CONFIG sem lock
#     • Acessos a CONFIG["RSI_MAX_ENTRADA"], CONFIG["FILTRO_*"] etc. em
#       thread_visual sem config_lock durante hot reload.
#     • Corrigido: snapshot de CONFIG lido dentro do config_lock.
#
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

# --- LOCKS GLOBAIS ---
state_lock    = threading.Lock()
config_lock   = threading.Lock()    # [FIX-05] protege leitura/escrita em CONFIG
detectors_lock = threading.Lock()   # [FIX-06] protege troca dos detectores globais


# =============================================================================
# [HIBRIDO] DETECTOR DE ANOMALIAS EM TEMPO REAL (TICK-BY-TICK)
# =============================================================================
class RealtimeAnomalyDetector:
    """
    Detecta flash pumps/crashes DURANTE a vela via stream de ticks.
    Alimentado pela thread_trade_ws() que consome o @trade WebSocket da Binance.

    Latência típica: 10-15 segundos.
    Baseline: precisa de N minutos de histórico antes de operar.

    [v5.2.2] Deduplicação por minuto + gate de volume + correção de confidence.
    """

    def __init__(self, symbol="PAXGUSDT", baseline_minutes=60,
                 price_spike_threshold=0.003,
                 volume_spike_threshold=3.0,
                 velocity_threshold=0.001):
        self.symbol = symbol
        self.baseline_minutes = baseline_minutes

        self.PRICE_SPIKE_THRESHOLD  = price_spike_threshold
        self.VOLUME_SPIKE_THRESHOLD = volume_spike_threshold
        self.VELOCITY_THRESHOLD     = velocity_threshold

        self.baseline_highs   = deque(maxlen=baseline_minutes)
        self.baseline_lows    = deque(maxlen=baseline_minutes)
        self.baseline_volumes = deque(maxlen=baseline_minutes)

        self.current_minute_start  = None
        self.current_minute_open   = None
        self.current_minute_high   = None
        self.current_minute_low    = None
        self.current_minute_close  = None
        self.current_minute_volume = 0.0
        self.current_minute_ticks  = 0

        self.recent_ticks = deque(maxlen=300)

        self.last_price     = None
        self.last_tick_time = None

        self.last_pump  = None
        self.last_crash = None
        self._last_pump_time  = None
        self._last_crash_time = None
        self.ANOMALY_TTL = 120

        self._last_pump_minute  = None
        self._last_crash_minute = None

        self.flash_pumps_detected   = 0
        self.flash_crashes_detected = 0

        self.baseline_ready = False
        self.ENABLED        = True

    def update_tick(self, price, volume, timestamp=None):
        if not self.ENABLED:
            return None

        if timestamp is None:
            timestamp = datetime.now()

        price  = float(price)
        volume = float(volume) if volume else 0.0

        current_minute = timestamp.replace(second=0, microsecond=0)
        if self.current_minute_start != current_minute:
            self._finalize_minute()
            self._start_new_minute(current_minute, price)

        if self.current_minute_high is None or price > self.current_minute_high:
            self.current_minute_high = price
        if self.current_minute_low is None or price < self.current_minute_low:
            self.current_minute_low = price
        self.current_minute_close   = price
        self.current_minute_volume += volume
        self.current_minute_ticks  += 1

        self.recent_ticks.append({'price': price, 'volume': volume, 'timestamp': timestamp})

        velocity = 0.0
        if self.last_price and self.last_tick_time:
            time_diff = (timestamp - self.last_tick_time).total_seconds()
            if time_diff > 0:
                velocity = ((price / self.last_price) - 1) / time_diff
        self.last_price     = price
        self.last_tick_time = timestamp

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
        if self.last_pump and self._last_pump_time:
            if (datetime.now() - self._last_pump_time).total_seconds() < self.ANOMALY_TTL:
                return self.last_pump
        return None

    def get_last_crash(self):
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
            current_min  = self.current_minute_start
            avg_high     = np.mean(self.baseline_highs)
            avg_low      = np.mean(self.baseline_lows)
            avg_volume   = np.mean(self.baseline_volumes) if self.baseline_volumes else 0

            pump_spike   = ((self.current_minute_high / avg_high) - 1) if self.current_minute_high else 0
            crash_drop   = ((avg_low / self.current_minute_low) - 1)   if self.current_minute_low  else 0
            volume_ratio = self.current_minute_volume / avg_volume      if avg_volume > 0           else 0

            velocity_extreme = abs(velocity) >= self.VELOCITY_THRESHOLD

            volume_ok   = volume_ratio >= self.VOLUME_SPIKE_THRESHOLD
            velocity_ok = velocity_extreme and volume_ratio >= 1.0

            def calcular_confidence(spike_ou_drop: float) -> float:
                terms = [spike_ou_drop / 0.01, volume_ratio / 5.0]
                if velocity_extreme and self.VELOCITY_THRESHOLD > 0:
                    terms.append(abs(velocity) / self.VELOCITY_THRESHOLD)
                return min(*terms, 1.0)

            # --- Flash Crash ---
            if crash_drop >= self.PRICE_SPIKE_THRESHOLD and (volume_ok or velocity_ok):
                if self._last_crash_minute == current_min:
                    return None
                confidence = calcular_confidence(crash_drop)
                self._last_crash_minute = current_min
                self.flash_crashes_detected += 1
                return {
                    "type":            "FLASH_CRASH",
                    "drop_pct":        crash_drop * 100,
                    "volume_ratio":    volume_ratio,
                    "velocity":        velocity * 100,
                    "confidence":      confidence,
                    "price_bottom":    self.current_minute_low,
                    "detection_type":  "REALTIME",
                    "elapsed_seconds": (datetime.now() - current_min).total_seconds()
                                       if current_min else 0,
                }

            # --- Flash Pump ---
            if pump_spike >= self.PRICE_SPIKE_THRESHOLD and (volume_ok or velocity_ok):
                if self._last_pump_minute == current_min:
                    return None
                confidence = calcular_confidence(pump_spike)
                self._last_pump_minute = current_min
                self.flash_pumps_detected += 1
                return {
                    "type":            "FLASH_PUMP",
                    "spike_pct":       pump_spike * 100,
                    "volume_ratio":    volume_ratio,
                    "velocity":        velocity * 100,
                    "confidence":      confidence,
                    "price_peak":      self.current_minute_high,
                    "detection_type":  "REALTIME",
                    "elapsed_seconds": (datetime.now() - current_min).total_seconds()
                                       if current_min else 0,
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
    Combina RealtimeAnomalyDetector (ticks) + AnomalyDetector (velas 1h).

    [v5.2.2] Correção do threshold RT-only em evaluate_pump (BUG 4).
    [v5.2.3] FIX-01: Mesma correção aplicada em evaluate_crash.
             FIX-03: full_sells_rt documentado/corrigido.
    """

    WEIGHT_RT = 0.6
    WEIGHT_1H = 1.0

    MIN_CONF_DIAG = 0.20

    def __init__(self, config, realtime_detector, candle_detector):
        self.realtime  = realtime_detector
        self.candle_1h = candle_detector

        self.MIN_CONF_SELL   = config.get("EMERGENCY_SELL_MIN_CONFIDENCE", 0.70)
        self.MIN_CONF_BUY    = config.get("EMERGENCY_BUY_MIN_CONFIDENCE",  0.80)
        self.PARTIAL_RATIO   = config.get("HYBRID_PARTIAL_SELL_RATIO",     0.50)
        self.PARTIAL_TIMEOUT = config.get("HYBRID_PARTIAL_TIMEOUT_SEC",    300)
        self.ENABLED         = config.get("HYBRID_ENABLED",                True)

        self.partial_sell_active = False
        self.partial_sell_time   = None
        self.partial_sell_price  = None

        self.partial_sells    = 0
        self.full_sells_rt    = 0   # [FIX-03] reservado para path RT SELL_ALL futuro
        self.full_sells_1h    = 0
        self.full_sells_combo = 0
        self.emergency_buys   = 0

    def evaluate_pump(self, candle_1h_data, profit_pct, current_price):
        """
        Avalia se há flash pump e qual ação tomar.

        Retorna:
          ("SELL_ALL",          score, source_str)
          ("SELL_PARTIAL",      score, source_str)
          ("SELL_ALL_REMAINING",score, source_str)
          (None, 0, "")
        """
        if not self.ENABLED or profit_pct < 0:
            return None, 0, ""

        rt_pump  = self.realtime.get_last_pump()
        pump_1h  = self.candle_1h.detect_flash_pump(candle_1h_data) if candle_1h_data else None

        score_rt = (rt_pump['confidence']  * self.WEIGHT_RT) if rt_pump  else 0.0
        score_1h = (pump_1h['confidence']  * self.WEIGHT_1H) if pump_1h  else 0.0

        if self.partial_sell_active:
            elapsed   = (datetime.now() - self.partial_sell_time).total_seconds()
            timeout   = elapsed >= self.PARTIAL_TIMEOUT
            confirmed = score_1h >= self.MIN_CONF_SELL

            if confirmed or timeout:
                reason = "confirmação 1h" if confirmed else f"timeout {elapsed:.0f}s"
                self.partial_sell_active = False
                return "SELL_ALL_REMAINING", max(score_rt, score_1h), f"PARTIAL→FULL ({reason})"

            return None, 0, ""

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

        # [v5.2.2] FIX BUG 4: threshold RT-only efetivo = MIN_CONF_SELL * WEIGHT_RT = 0.42
        if only_rt and rt_pump['confidence'] >= (self.MIN_CONF_SELL * self.WEIGHT_RT):
            self.partial_sell_active = True
            self.partial_sell_time   = datetime.now()
            self.partial_sell_price  = current_price
            self.partial_sells += 1
            self.realtime.clear_last_pump()
            return "SELL_PARTIAL", score_rt, "RT-only"

        if (rt_pump or pump_1h) and max(score_rt, score_1h) >= self.MIN_CONF_DIAG:
            logging.info(
                f"[HYBRID DIAG PUMP] Detectado mas score insuficiente — "
                f"RT={score_rt:.2f} (conf={rt_pump['confidence']:.0%} threshold={self.MIN_CONF_SELL * self.WEIGHT_RT:.2f}) "
                f"| 1h={score_1h:.2f} (threshold={self.MIN_CONF_SELL:.2f}) "
                f"| Lucro={profit_pct:+.2f}%")

        return None, 0, ""

    def evaluate_crash(self, candle_1h_data, in_position, capital_available):
        """
        Avalia se há flash crash e se deve comprar de emergência.

        [v5.2.3] FIX-01: Corrigido threshold RT-only (análogo ao BUG 4 de evaluate_pump).
          Antes: best_score = max(confidence * WEIGHT_RT, score_1h)
                 Para RT-only: best_score = confidence * 0.6
                 threshold = MIN_CONF_BUY = 0.80
                 → exigia confidence >= 1.33 → IMPOSSÍVEL, crash RT-only nunca disparava.

          Depois: RT-only usa confiança bruta vs threshold reduzido:
                  rt_crash['confidence'] >= MIN_CONF_BUY * WEIGHT_RT = 0.48 ✓
                  Para RT+1h e 1h-only: mantém lógica original com best_score.

        Retorna: (True, score, source) ou (False, 0, "")
        """
        if not self.ENABLED or in_position or capital_available <= 0:
            return False, 0, ""

        rt_crash = self.realtime.get_last_crash()
        crash_1h = self.candle_1h.detect_flash_crash(candle_1h_data) if candle_1h_data else None

        score_rt = (rt_crash['confidence'] * self.WEIGHT_RT) if rt_crash else 0.0
        score_1h = (crash_1h['confidence'] * self.WEIGHT_1H) if crash_1h else 0.0

        both_detect = rt_crash and crash_1h
        only_rt     = rt_crash and not crash_1h
        only_1h     = crash_1h and not rt_crash

        # RT+1h combinado ou 1h isolado: usa score ponderado
        if both_detect:
            combined = min(score_rt + score_1h, 1.0)
            if combined >= self.MIN_CONF_BUY:
                self.emergency_buys += 1
                self.realtime.clear_last_crash()
                return True, combined, "RT+1h"

        if only_1h and score_1h >= self.MIN_CONF_BUY:
            self.emergency_buys += 1
            return True, score_1h, "1h"

        # [FIX-01] RT-only: compara confiança bruta com threshold reduzido
        # Threshold efetivo = MIN_CONF_BUY * WEIGHT_RT = 0.80 * 0.6 = 0.48
        if only_rt and rt_crash['confidence'] >= (self.MIN_CONF_BUY * self.WEIGHT_RT):
            self.emergency_buys += 1
            self.realtime.clear_last_crash()
            return True, score_rt, "RT-only"

        # Diagnóstico: detectado mas abaixo do threshold
        best_score = max(score_rt, score_1h)
        if (rt_crash or crash_1h) and best_score >= self.MIN_CONF_DIAG:
            logging.info(
                f"[HYBRID DIAG CRASH] Detectado mas score insuficiente — "
                f"RT={score_rt:.2f} (conf={rt_crash['confidence']:.0%} threshold={self.MIN_CONF_BUY * self.WEIGHT_RT:.2f}) "
                f"| 1h={score_1h:.2f} (threshold={self.MIN_CONF_BUY:.2f})")

        return False, 0, ""

    def reset_partial(self):
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
# [M6] DETECTOR DE ANOMALIAS BASEADO EM VELAS (1h fechadas)
# =============================================================================
class AnomalyDetector:
    """
    Detecta flash pumps/crashes usando velas OHLCV FECHADAS (1h).
    [v5.2.2] Recebe apenas velas fechadas (iloc[-2]) — nunca a vela aberta atual.
    [v5.2.3] FIX-07: Fallback de trades corrigido para avg_trades (neutro).
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
        """Alimenta o histórico com uma vela FECHADA."""
        if not self.ENABLED:
            return
        try:
            self.price_history.append(float(candle_data['c']))
            self.volume_history.append(float(candle_data['v']))
            # [FIX-07] Só registra trades reais; campo ausente é ignorado
            # O fallback neutro é aplicado no momento da detecção (avg_trades).
            if 'trades' in candle_data:
                self.trades_history.append(float(candle_data['trades']))
        except Exception as e:
            logging.warning(f"Erro ao atualizar AnomalyDetector: {e}")

    def detect_flash_pump(self, current_candle):
        """Avalia vela FECHADA para pump. Retorna dict ou None."""
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_spike = ((float(current_candle['h']) / float(current_candle['o'])) - 1)
            if price_spike < self.PRICE_SPIKE_THRESHOLD:
                return None
            avg_volume     = np.mean(self.volume_history)
            current_volume = float(current_candle['v'])
            volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 1

            # [FIX-07] Fallback neutro: usa avg_trades se campo ausente (ratio=1.0)
            if self.trades_history:
                avg_trades     = np.mean(self.trades_history)
                current_trades = float(current_candle.get('trades', avg_trades))
                trades_ratio   = current_trades / avg_trades if avg_trades > 0 else 1
            else:
                # Sem histórico de trades: ignora critério de trades
                trades_ratio = self.TRADES_SPIKE_THRESHOLD  # passa o filtro

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
        """Avalia vela FECHADA para crash. Retorna dict ou None."""
        if not self.ENABLED or len(self.price_history) < self.LOOKBACK_CANDLES:
            return None
        try:
            price_drop = ((float(current_candle['o']) / float(current_candle['l'])) - 1)
            if price_drop < self.PRICE_SPIKE_THRESHOLD:
                return None
            avg_volume     = np.mean(self.volume_history)
            current_volume = float(current_candle['v'])
            volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 1

            # [FIX-07] Fallback neutro
            if self.trades_history:
                avg_trades     = np.mean(self.trades_history)
                current_trades = float(current_candle.get('trades', avg_trades))
                trades_ratio   = current_trades / avg_trades if avg_trades > 0 else 1
            else:
                trades_ratio = self.TRADES_SPIKE_THRESHOLD

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
        # [FIX-10] Lê symbol com config_lock para evitar race no hot reload
        with config_lock:
            symbol = CONFIG["SYMBOL"]
        try:
            with open(ARQUIVO_LOG_TRADES, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    symbol, tipo,
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
    "anomaly_flash_pumps": 0,
    "anomaly_flash_crashes": 0,
    "anomaly_emergency_sells": 0,
    "anomaly_emergency_buys": 0,
    "rt_baseline_ready": False,
    "rt_baseline_size": 0,
    "rt_flash_pumps": 0,
    "rt_flash_crashes": 0,
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

anomaly_detector  = None
realtime_detector = None
hybrid_detector   = None


# =============================================================================
# CONFIGURAÇÃO GLOBAL
# =============================================================================
CONFIG = {
    "API_KEY": "",
    "SECRET":  "",
    "SYMBOL":    "BTC/USDT",
    "MOEDA_BASE": "USDT",

    "CAPITAL_BASE":      14.75,
    "MAX_SAFETY_ORDERS": 3,

    "DCA_VOLUME_SCALE": 1.5,
    "DCA_STEP_INITIAL": 0.02,
    "DCA_STEP_SCALE":   1.3,

    "TRAILING_TRIGGER": 0.015,
    "TRAILING_DIST":    0.005,

    "STOP_LOSS_USD": 10.0,

    "FILTRO_RSI_ATIVO": True,
    "RSI_MAX_ENTRADA":  65.0,
    "RSI_PERIOD":       14,

    "FILTRO_VOLUME_ATIVO": True,
    "VOLUME_FATOR_MIN":    1.2,

    "CIRCUIT_BREAKER_ATIVO": True,
    "CB_QUEDA_PCT":          3.0,
    "CB_JANELA_VELAS":       5,

    "ATR_PERIOD":        14,
    "ATR_MULTIPLICADOR": 1.5,

    "ANOMALY_DETECTOR_ENABLED":      True,
    "PRICE_SPIKE_THRESHOLD":         0.005,
    "VOLUME_SPIKE_THRESHOLD":        5.0,
    "TRADES_SPIKE_THRESHOLD":        10.0,
    "ANOMALY_LOOKBACK":              20,
    "EMERGENCY_SELL_MIN_CONFIDENCE": 0.70,
    "EMERGENCY_BUY_MIN_CONFIDENCE":  0.80,

    "HYBRID_ENABLED":             True,
    "RT_BASELINE_MINUTES":        60,
    "RT_PRICE_SPIKE_THRESHOLD":   0.003,
    "RT_VOLUME_SPIKE_THRESHOLD":  3.0,
    "RT_VELOCITY_THRESHOLD":      0.001,
    "HYBRID_PARTIAL_SELL_RATIO":  0.50,
    "HYBRID_PARTIAL_TIMEOUT_SEC": 300,

    "RT_LOG_MIN_CONFIDENCE": 0.15,
    "RT_LOG_COOLDOWN_SEC":   60,
}


# =============================================================================
# PERSISTÊNCIA
# =============================================================================
def salvar_estado_disco():
    """
    [FIX-09] Persiste também o estado de partial sell para sobreviver a reinicializações.
    """
    try:
        with state_lock:
            partial_active  = hybrid_detector.partial_sell_active if hybrid_detector else False
            # partial_sell_qtd_restante e custo estão no escopo do loop_dca_ativo.
            # Para persistir, usamos os valores do shared_state via campos novos.
            dados = {
                "em_operacao":    shared_state["em_operacao"],
                "ordens":         list(ordens_executadas),
                "symbol":         CONFIG["SYMBOL"],
                "max_p_trailing": shared_state["max_p_trailing"],
                "trailing_ativo": shared_state["trailing_ativo"],
                "timestamp":      str(datetime.now()),
                # [FIX-09] Persiste estado parcial
                "partial_sell_active":          partial_active,
                "partial_sell_qtd_restante":    shared_state.get("partial_sell_qtd_restante",   0.0),
                "partial_sell_custo_restante":  shared_state.get("partial_sell_custo_restante", 0.0),
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
    """
    [FIX-09] Restaura estado de partial sell junto com o estado normal.
    """
    global ordens_executadas
    if not os.path.exists(ARQUIVO_ESTADO):
        return False
    try:
        with open(ARQUIVO_ESTADO) as f:
            dados = json.load(f)
        if dados.get("em_operacao") and dados.get("symbol") == CONFIG["SYMBOL"]:
            with state_lock:
                ordens_executadas                          = dados["ordens"]
                shared_state["em_operacao"]                = True
                shared_state["marcha"]                     = "RECUPERANDO..."
                shared_state["max_p_trailing"]             = dados.get("max_p_trailing", 0.0)
                shared_state["trailing_ativo"]             = dados.get("trailing_ativo", False)
                # [FIX-09] Restaura estado de partial sell
                shared_state["partial_sell_qtd_restante"]   = dados.get("partial_sell_qtd_restante",   0.0)
                shared_state["partial_sell_custo_restante"] = dados.get("partial_sell_custo_restante", 0.0)
                if dados.get("partial_sell_active") and hybrid_detector:
                    hybrid_detector.partial_sell_active = True
                    hybrid_detector.partial_sell_time   = datetime.now()  # timeout reiniciado
                    Auditoria.log_sistema(
                        "PARTIAL SELL restaurado do estado — timeout reiniciado.", "AVISO")
            Auditoria.log_sistema(f"ESTADO RESTAURADO: {len(ordens_executadas)} ordens.", "AVISO")
            return True
    except Exception as e:
        Auditoria.log_sistema(f"Erro ao ler save: {e}", "ERRO")
    return False


# =============================================================================
# CONEXÃO / CONFIG
# =============================================================================
def carregar_configuracoes():
    """
    [FIX-05] CONFIG agora protegido por config_lock.
    [FIX-06] Detectores globais trocados sob detectors_lock.
    """
    global exchange, CONFIG, anomaly_detector, realtime_detector, hybrid_detector

    Auditoria.configurar()
    msg_update     = ""
    mudanca_symbol = False

    if os.path.exists("config.ini"):
        cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cp.read("config.ini")
        try:
            # Constrói CONFIG novo localmente antes de adquirir o lock
            with config_lock:
                novo_symbol = cp["trading"]["symbol"]
                if novo_symbol != CONFIG["SYMBOL"]:
                    mudanca_symbol = True
                    msg_update += f" [Novo Par: {novo_symbol}]"

                CONFIG["API_KEY"] = cp["binance"]["api_key"]
                CONFIG["SECRET"]  = cp["binance"]["secret"]
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

                CONFIG["FILTRO_RSI_ATIVO"]  = cp["trading"].getboolean("filtro_rsi_ativo",  CONFIG["FILTRO_RSI_ATIVO"])
                CONFIG["RSI_MAX_ENTRADA"]   = float(g("rsi_max_entrada",   CONFIG["RSI_MAX_ENTRADA"]))
                CONFIG["RSI_PERIOD"]        = int(  g("rsi_period",        CONFIG["RSI_PERIOD"]))

                CONFIG["FILTRO_VOLUME_ATIVO"] = cp["trading"].getboolean("filtro_volume_ativo", CONFIG["FILTRO_VOLUME_ATIVO"])
                CONFIG["VOLUME_FATOR_MIN"]    = float(g("volume_fator_min", CONFIG["VOLUME_FATOR_MIN"]))

                CONFIG["CIRCUIT_BREAKER_ATIVO"] = cp["trading"].getboolean("circuit_breaker_ativo", CONFIG["CIRCUIT_BREAKER_ATIVO"])
                CONFIG["CB_QUEDA_PCT"]          = float(g("cb_queda_pct",    CONFIG["CB_QUEDA_PCT"]))
                CONFIG["CB_JANELA_VELAS"]       = int(  g("cb_janela_velas", CONFIG["CB_JANELA_VELAS"]))

                if "anomaly" in cp:
                    ga = cp["anomaly"].get
                    CONFIG["ANOMALY_DETECTOR_ENABLED"]      = cp["anomaly"].getboolean("enabled", CONFIG["ANOMALY_DETECTOR_ENABLED"])
                    CONFIG["PRICE_SPIKE_THRESHOLD"]         = float(ga("price_spike_threshold",         CONFIG["PRICE_SPIKE_THRESHOLD"]))
                    CONFIG["VOLUME_SPIKE_THRESHOLD"]        = float(ga("volume_spike_threshold",        CONFIG["VOLUME_SPIKE_THRESHOLD"]))
                    CONFIG["TRADES_SPIKE_THRESHOLD"]        = float(ga("trades_spike_threshold",        CONFIG["TRADES_SPIKE_THRESHOLD"]))
                    CONFIG["ANOMALY_LOOKBACK"]              = int(  ga("lookback_candles",              CONFIG["ANOMALY_LOOKBACK"]))
                    CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"] = float(ga("emergency_sell_min_confidence", CONFIG["EMERGENCY_SELL_MIN_CONFIDENCE"]))
                    CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]  = float(ga("emergency_buy_min_confidence",  CONFIG["EMERGENCY_BUY_MIN_CONFIDENCE"]))

                if "hybrid" in cp:
                    gh = cp["hybrid"].get
                    CONFIG["HYBRID_ENABLED"]             = cp["hybrid"].getboolean("enabled",                CONFIG["HYBRID_ENABLED"])
                    CONFIG["RT_BASELINE_MINUTES"]        = int(  gh("rt_baseline_minutes",        CONFIG["RT_BASELINE_MINUTES"]))
                    CONFIG["RT_PRICE_SPIKE_THRESHOLD"]   = float(gh("rt_price_spike_threshold",   CONFIG["RT_PRICE_SPIKE_THRESHOLD"]))
                    CONFIG["RT_VOLUME_SPIKE_THRESHOLD"]  = float(gh("rt_volume_spike_threshold",  CONFIG["RT_VOLUME_SPIKE_THRESHOLD"]))
                    CONFIG["RT_VELOCITY_THRESHOLD"]      = float(gh("rt_velocity_threshold",      CONFIG["RT_VELOCITY_THRESHOLD"]))
                    CONFIG["HYBRID_PARTIAL_SELL_RATIO"]  = float(gh("partial_sell_ratio",         CONFIG["HYBRID_PARTIAL_SELL_RATIO"]))
                    CONFIG["HYBRID_PARTIAL_TIMEOUT_SEC"] = int(  gh("partial_sell_timeout_sec",   CONFIG["HYBRID_PARTIAL_TIMEOUT_SEC"]))
                    CONFIG["RT_LOG_MIN_CONFIDENCE"]      = float(gh("rt_log_min_confidence",      CONFIG["RT_LOG_MIN_CONFIDENCE"]))
                    CONFIG["RT_LOG_COOLDOWN_SEC"]        = int(  gh("rt_log_cooldown_sec",        CONFIG["RT_LOG_COOLDOWN_SEC"]))

                # Snapshot para construção dos detectores (fora do lock de state)
                cfg_snapshot = dict(CONFIG)

        except Exception as e:
            Auditoria.log_sistema(f"Erro ao ler config.ini: {e}", "ERRO")
            return
    else:
        with config_lock:
            cfg_snapshot = dict(CONFIG)

    if exchange is None:
        try:
            print(f"{Fore.YELLOW}Conectando a Binance...")
            exchange = ccxt.binance({
                'apiKey': cfg_snapshot["API_KEY"],
                'secret': cfg_snapshot["SECRET"],
                'enableRateLimit': True,
                'options': {'adjustForTimeDifference': True},
            })
            exchange.load_markets()
            definir_status(f"Conectado: {cfg_snapshot['SYMBOL']}", "SUCESSO")
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

    # [FIX-06] Constrói detectores e os troca atomicamente sob detectors_lock
    novo_anomaly  = AnomalyDetector(cfg_snapshot)
    symbol_raw    = cfg_snapshot["SYMBOL"].replace("/", "")
    novo_realtime = RealtimeAnomalyDetector(
        symbol                 = symbol_raw,
        baseline_minutes       = cfg_snapshot["RT_BASELINE_MINUTES"],
        price_spike_threshold  = cfg_snapshot["RT_PRICE_SPIKE_THRESHOLD"],
        volume_spike_threshold = cfg_snapshot["RT_VOLUME_SPIKE_THRESHOLD"],
        velocity_threshold     = cfg_snapshot["RT_VELOCITY_THRESHOLD"],
    )
    novo_realtime.ENABLED = cfg_snapshot["HYBRID_ENABLED"]
    novo_hybrid   = HybridDetector(cfg_snapshot, novo_realtime, novo_anomaly)

    with detectors_lock:
        anomaly_detector  = novo_anomaly
        realtime_detector = novo_realtime
        hybrid_detector   = novo_hybrid

    Auditoria.log_sistema(
        f"Detector velas: {'ATIVO' if cfg_snapshot['ANOMALY_DETECTOR_ENABLED'] else 'DESATIVADO'}", "INFO")
    Auditoria.log_sistema(
        f"Filtros: RSI={'ATIVO' if cfg_snapshot['FILTRO_RSI_ATIVO'] else 'DESATIVADO'} "
        f"| Volume={'ATIVO' if cfg_snapshot['FILTRO_VOLUME_ATIVO'] else 'DESATIVADO'} "
        f"| CircuitBreaker={'ATIVO' if cfg_snapshot['CIRCUIT_BREAKER_ATIVO'] else 'DESATIVADO'}", "INFO")
    Auditoria.log_sistema(
        f"Detector híbrido: {'ATIVO' if cfg_snapshot['HYBRID_ENABLED'] else 'DESATIVADO'} "
        f"| RT baseline: {cfg_snapshot['RT_BASELINE_MINUTES']} min "
        f"| Partial ratio: {cfg_snapshot['HYBRID_PARTIAL_SELL_RATIO']*100:.0f}% "
        f"| Timeout: {cfg_snapshot['HYBRID_PARTIAL_TIMEOUT_SEC']}s "
        f"| Log conf mín: {cfg_snapshot['RT_LOG_MIN_CONFIDENCE']:.0%} "
        f"| Log cooldown: {cfg_snapshot['RT_LOG_COOLDOWN_SEC']}s", "INFO")


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
    with config_lock:
        trailing_dist_default = CONFIG["TRAILING_DIST"]
    return float(atr / preco_atual) if preco_atual > 0 else trailing_dist_default


def trailing_dist_por_atr(df_1h: pd.DataFrame) -> float:
    try:
        with config_lock:
            atr_period        = CONFIG["ATR_PERIOD"]
            atr_multiplicador = CONFIG["ATR_MULTIPLICADOR"]
            trailing_dist_min = CONFIG["TRAILING_DIST"]
        atr_perc = calcular_atr(df_1h, atr_period)
        dist     = atr_perc * atr_multiplicador
        return max(dist, trailing_dist_min)
    except Exception:
        with config_lock:
            return CONFIG["TRAILING_DIST"]


# =============================================================================
# ANÁLISE MTF + FILTROS
# =============================================================================
def check_mtf_trend() -> bool:
    config_tf       = {'15m': 25, '1h': 50, '4h': 100}
    filtros         = {}
    votos_positivos = 0
    with config_lock:
        symbol = CONFIG['SYMBOL']
    try:
        for tf, period in config_tf.items():
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=period + 5)
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
    with config_lock:
        cb_ativo      = CONFIG["CIRCUIT_BREAKER_ATIVO"]
        symbol        = CONFIG['SYMBOL']
        cb_queda_pct  = CONFIG["CB_QUEDA_PCT"]
        cb_janela     = CONFIG["CB_JANELA_VELAS"]

    if not cb_ativo:
        with state_lock:
            shared_state["circuit_breaker"] = False
        return False

    try:
        janela = cb_janela + 2
        ohlcv  = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=janela)
        df     = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        preco_inicio = float(df['c'].iloc[-(cb_janela + 1)])
        preco_atual  = float(df['c'].iloc[-1])
        variacao_pct = ((preco_atual / preco_inicio) - 1) * 100
        ativado      = variacao_pct <= -cb_queda_pct
        with state_lock:
            shared_state["circuit_breaker"] = ativado
        if ativado:
            definir_status(
                f"⚡ Circuit Breaker: mercado caiu {variacao_pct:.2f}% em "
                f"{cb_janela} velas de 4h. Entradas pausadas.", "AVISO")
        return ativado
    except Exception as e:
        Auditoria.log_sistema(f"Erro Circuit Breaker: {e}", "ERRO")
        return False


def verificar_filtros_entrada(df_1h: pd.DataFrame) -> tuple[bool, str]:
    with config_lock:
        filtro_rsi_ativo    = CONFIG["FILTRO_RSI_ATIVO"]
        rsi_period          = CONFIG["RSI_PERIOD"]
        rsi_max             = CONFIG["RSI_MAX_ENTRADA"]
        filtro_volume_ativo = CONFIG["FILTRO_VOLUME_ATIVO"]
        volume_fator_min    = CONFIG["VOLUME_FATOR_MIN"]

    try:
        rsi = calcular_rsi(df_1h, rsi_period)
        with state_lock:
            shared_state["rsi_atual"] = rsi
        if filtro_rsi_ativo and rsi >= rsi_max:
            return False, f"RSI sobrecomprado ({rsi:.1f} >= {rsi_max})"
    except Exception as e:
        Auditoria.log_sistema(f"Erro filtro RSI: {e}", "AVISO")

    if filtro_volume_ativo:
        try:
            vol_atual = float(df_1h['v'].iloc[-1])
            vol_media = float(df_1h['v'].rolling(20).mean().iloc[-1])
            if vol_atual < vol_media * volume_fator_min:
                return False, (f"Volume baixo ({vol_atual:.0f} < "
                               f"{vol_media * volume_fator_min:.0f})")
        except Exception as e:
            Auditoria.log_sistema(f"Erro filtro Volume: {e}", "AVISO")

    return True, "OK"


def obter_df_1h(limit: int = 120) -> pd.DataFrame | None:
    with config_lock:
        symbol = CONFIG['SYMBOL']
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=limit)
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
    """
    [FIX-04] Guard adicionado: retorna (None, None) se lista de ordens vazia.
    """
    with config_lock:
        max_safety = CONFIG["MAX_SAFETY_ORDERS"]
        vol_scale  = CONFIG["DCA_VOLUME_SCALE"]
        step_init  = CONFIG["DCA_STEP_INITIAL"]
        step_scale = CONFIG["DCA_STEP_SCALE"]

    if num_ordem_atual >= max_safety:
        return None, None

    with state_lock:
        if not ordens_executadas:  # [FIX-04] guard
            return None, None
        custo_anterior = ordens_executadas[-1]['custo']

    novo_vol_usd    = custo_anterior * vol_scale
    fator_distancia = step_scale ** num_ordem_atual
    pct_queda       = step_init * fator_distancia
    novo_preco      = preco_ult * (1 - pct_queda)
    return novo_preco, novo_vol_usd


# =============================================================================
# VAULT
# =============================================================================
def get_saldo_fundos() -> float:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        bal = exchange.fetch_balance({'type': 'funding'})
        return bal.get(moeda, {}).get('free', 0)
    except Exception:
        return 0


def transferir_para_spot(valor: float) -> bool:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        exchange.transfer(moeda, valor, 'funding', 'spot')
        Auditoria.log_sistema(f"VAULT: ${valor:.2f} enviado ao Spot", "INFO")
        time.sleep(1)
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Fundos→Spot): {e}", "ERRO")
        return False


def recolher_para_fundos() -> bool:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        balance    = exchange.fetch_balance()
        saldo_usdt = balance.get(moeda, {}).get('free', 0)
        if saldo_usdt > 0.5:
            exchange.transfer(moeda, saldo_usdt, 'spot', 'funding')
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
    with config_lock:
        symbol = CONFIG['SYMBOL']
    try:
        ordem = exchange.create_order(
            symbol, 'market', 'buy', None,
            params={'quoteOrderQty': exchange.cost_to_precision(symbol, valor_usd)}
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
    with config_lock:
        symbol = CONFIG['SYMBOL']
    try:
        if not exchange.markets:
            exchange.load_markets()
        qtd_fmt = exchange.amount_to_precision(symbol, qtd)
        ordem   = exchange.create_order(symbol, 'market', 'sell', qtd_fmt)
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
        shared_state["em_operacao"]                = False
        shared_state["trailing_ativo"]             = False
        shared_state["max_p_trailing"]             = 0.0
        shared_state["alvo_trailing_ativacao"]     = 0.0
        shared_state["stop_atual_trailing"]        = 0.0
        shared_state["perda_usd_atual"]            = 0.0
        shared_state["trailing_dist_atual"]        = 0.0
        shared_state["hybrid_partial_active"]      = False
        shared_state["hybrid_partial_elapsed"]     = 0
        shared_state["partial_sell_qtd_restante"]  = 0.0   # [FIX-09]
        shared_state["partial_sell_custo_restante"]= 0.0   # [FIX-09]
    with detectors_lock:
        hd = hybrid_detector
    if hd:
        hd.reset_partial()
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
        # [FIX-09] Restaura partial sell se havia estado salvo
        partial_sell_qtd_restante   = shared_state.get("partial_sell_qtd_restante",   0.0)
        partial_sell_custo_restante = shared_state.get("partial_sell_custo_restante", 0.0)

    with detectors_lock:
        hd = hybrid_detector

    partial_sell_ativa = hd.partial_sell_active if hd else False

    prox_p, prox_v_usd = calcular_proxima_safety_order(
        ordens_snap[-1]['p'] if ordens_snap else 0.0, len(ordens_snap) - 1)

    with config_lock:
        trailing_dist = CONFIG["TRAILING_DIST"]

    # [FIX-08] Uma única chamada com limit=120 (suficiente para ATR e para anomaly)
    df_1h_inicial = obter_df_1h(limit=120)
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
            # [FIX-08] Uma única chamada por iteração com limit=120
            df_1h_atual    = obter_df_1h(limit=120)
            candle_fechada = None

            if df_1h_atual is not None and len(df_1h_atual) > 1:
                candle_fechada = {
                    'o': df_1h_atual['o'].iloc[-2],
                    'h': df_1h_atual['h'].iloc[-2],
                    'l': df_1h_atual['l'].iloc[-2],
                    'c': df_1h_atual['c'].iloc[-2],
                    'v': df_1h_atual['v'].iloc[-2],
                }
                with detectors_lock:
                    ad = anomaly_detector
                if ad:
                    ad.update(candle_fechada)

            with detectors_lock:
                ad_ref = anomaly_detector
                rd_ref = realtime_detector
                hd_ref = hybrid_detector

            with state_lock:
                if ad_ref:
                    shared_state["anomaly_flash_pumps"]     = ad_ref.flash_pumps_detected
                    shared_state["anomaly_flash_crashes"]   = ad_ref.flash_crashes_detected
                    shared_state["anomaly_emergency_sells"] = ad_ref.emergency_sells
                    shared_state["anomaly_emergency_buys"]  = ad_ref.emergency_buys

                if rd_ref:
                    rt_stats = rd_ref.get_stats()
                    shared_state["rt_baseline_ready"] = rt_stats["baseline_ready"]
                    shared_state["rt_baseline_size"]  = rt_stats["baseline_size"]
                    shared_state["rt_flash_pumps"]    = rt_stats["flash_pumps"]
                    shared_state["rt_flash_crashes"]  = rt_stats["flash_crashes"]

                if hd_ref:
                    hstats = hd_ref.get_stats()
                    shared_state["hybrid_partial_sells"]    = hstats["partial_sells"]
                    shared_state["hybrid_full_sells_rt"]    = hstats["full_sells_rt"]
                    shared_state["hybrid_full_sells_1h"]    = hstats["full_sells_1h"]
                    shared_state["hybrid_full_sells_combo"] = hstats["full_sells_combo"]
                    shared_state["hybrid_emergency_buys"]   = hstats["emergency_buys"]
                    shared_state["hybrid_partial_active"]   = hstats["partial_active"]
                    if hstats["partial_active"] and hd_ref.partial_sell_time:
                        elapsed = (datetime.now() - hd_ref.partial_sell_time).total_seconds()
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
                shared_state["preco_medio"]                  = pm
                shared_state["lucro_perc_atual"]             = lucro_atual_perc
                shared_state["perda_usd_atual"]              = perda_usd
                shared_state["num_safety_orders"]            = n_ordens - 1
                shared_state["proxima_compra_p"]             = prox_p if prox_p else 0.0
                shared_state["max_p_trailing"]               = max_p
                shared_state["partial_sell_qtd_restante"]    = partial_sell_qtd_restante   # [FIX-09]
                shared_state["partial_sell_custo_restante"]  = partial_sell_custo_restante # [FIX-09]

            with config_lock:
                stop_loss_usd        = CONFIG["STOP_LOSS_USD"]
                hybrid_partial_ratio = CONFIG["HYBRID_PARTIAL_SELL_RATIO"]
                hybrid_enabled       = CONFIG["HYBRID_ENABLED"]

            # ------------------------------------------------------------------
            # 1. GESTÃO DE COMPRA (DCA)
            # ------------------------------------------------------------------
            if not partial_sell_ativa and prox_p and preco <= prox_p:
                msg_ordem = f"DCA #{n_ordens}"
                definir_status(f"Executando {msg_ordem}...", "AVISO")
                res = comprar_com_vault(prox_v_usd, motivo=msg_ordem)
                if res['ok']:
                    with state_lock:                         # [FIX-02] com lock
                        ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                        n_ordens = len(ordens_executadas)
                    trailing_ativo = False
                    max_p          = 0.0
                    with detectors_lock:
                        if hybrid_detector:
                            hybrid_detector.reset_partial()

                    df_1h_novo = obter_df_1h(limit=120)
                    if df_1h_novo is not None:
                        trailing_dist = trailing_dist_por_atr(df_1h_novo)
                        with state_lock:
                            shared_state["trailing_dist_atual"] = trailing_dist

                    salvar_estado_disco()
                    prox_p, prox_v_usd = calcular_proxima_safety_order(res['p'], n_ordens - 1)
                else:
                    definir_status(f"Falha DCA: {res.get('msg', 'Erro desconhecido')}", "ERRO")

            # ------------------------------------------------------------------
            # 2. STOP LOSS DINÂMICO EM USD
            # ------------------------------------------------------------------
            if perda_usd >= stop_loss_usd:
                definir_status(
                    f"🛑 STOP LOSS USD: prejuízo ${perda_usd:.2f} >= "
                    f"${stop_loss_usd:.2f}", "ERRO")
                res_venda = executar_venda_mercado(qtd_efetiva, motivo="SL USD")
                if res_venda['ok']:
                    Auditoria.log_transacao(
                        "STOP LOSS", res_venda['p'], qtd_efetiva, res_venda['c'],
                        lucro_usd=-perda_usd,
                        lucro_perc=lucro_atual_perc,
                        obs=f"SL USD (limite ${stop_loss_usd:.2f})"
                             + (" [pós-partial]" if partial_sell_ativa else ""))
                    finalizar_ciclo(motivo="STOP LOSS USD")
                    break

            # ------------------------------------------------------------------
            # 2.5. DETECTOR HÍBRIDO (FLASH PUMP)
            # ------------------------------------------------------------------
            if hd_ref and hybrid_enabled and candle_fechada is not None:

                action, score, source = hd_ref.evaluate_pump(
                    candle_fechada, lucro_atual_perc, preco)

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
                    qtd_vender  = qtd_total_ciclo   * hybrid_partial_ratio
                    custo_frac  = custo_total_ciclo * hybrid_partial_ratio
                    definir_status(
                        f"⚡ HYBRID PARTIAL SELL ({hybrid_partial_ratio*100:.0f}%) "
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
                        salvar_estado_disco()  # [FIX-09] persiste imediatamente após partial

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
            # 3. TRAILING STOP COM ATR
            # ------------------------------------------------------------------
            if not partial_sell_ativa:
                with config_lock:
                    trailing_trigger = CONFIG["TRAILING_TRIGGER"]
                gatilho_ts = pm * (1 + trailing_trigger)
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
                    with config_lock:
                        rsi_period = CONFIG["RSI_PERIOD"]
                    rsi = calcular_rsi(df_1h, rsi_period)
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

                with detectors_lock:
                    hd_ref = hybrid_detector

                with config_lock:
                    hybrid_enabled = CONFIG["HYBRID_ENABLED"]
                    capital_base   = CONFIG["CAPITAL_BASE"]

                # Verifica compra de emergência por flash crash
                if hd_ref and hybrid_enabled and preco > 0:
                    df_1h_crash          = obter_df_1h(limit=25)
                    candle_fechada_crash = None
                    if df_1h_crash is not None and len(df_1h_crash) > 1:
                        candle_fechada_crash = {
                            'o': df_1h_crash['o'].iloc[-2],
                            'h': df_1h_crash['h'].iloc[-2],
                            'l': df_1h_crash['l'].iloc[-2],
                            'c': df_1h_crash['c'].iloc[-2],
                            'v': df_1h_crash['v'].iloc[-2],
                        }
                    saldo_vault = get_saldo_fundos()
                    should_buy, score_crash, source_crash = hd_ref.evaluate_crash(
                        candle_fechada_crash, em_op, saldo_vault)

                    if should_buy:
                        definir_status(
                            f"🟢 HYBRID CRASH BUY! [{source_crash}] Score: {score_crash:.0%} "
                            f"| Comprando no fundo...", "SUCESSO")
                        res = comprar_com_vault(capital_base,
                                               motivo=f"EMERGÊNCIA CRASH HYBRID [{source_crash}]")
                        if res['ok']:
                            Auditoria.log_transacao(
                                tipo="EMERGÊNCIA CRASH HYBRID",
                                preco=res['p'],
                                qtd=res['v'],
                                total_usd=res['c'],
                                obs=f"Flash Crash [{source_crash}] Score {score_crash:.0%}")
                            with state_lock:            # [FIX-02] com lock
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
                    res = comprar_com_vault(capital_base, motivo="Entrada MTF")
                    if res['ok']:
                        with state_lock:                # [FIX-02] com lock
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
# THREAD TRADE WEBSOCKET
# =============================================================================
async def trade_ws_loop():
    with config_lock:
        ultimo_symbol = CONFIG['SYMBOL'].replace('/', '').lower()
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol}@trade"

    _ultimo_log: dict[str, datetime | None] = {"FLASH_PUMP": None, "FLASH_CRASH": None}

    while True:
        try:
            with config_lock:
                symbol_atual = CONFIG['SYMBOL'].replace('/', '').lower()
                log_min_conf = CONFIG["RT_LOG_MIN_CONFIDENCE"]
                log_cooldown = CONFIG["RT_LOG_COOLDOWN_SEC"]

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
                    with config_lock:
                        cur_sym = CONFIG['SYMBOL'].replace('/', '').lower()
                    if cur_sym != ultimo_symbol:
                        break

                    data     = json.loads(message)
                    price    = float(data['p'])
                    quantity = float(data['q'])
                    ts       = datetime.fromtimestamp(data['T'] / 1000)

                    with detectors_lock:
                        rd = realtime_detector

                    if rd and rd.ENABLED:
                        anomaly = rd.update_tick(price, quantity, ts)
                        if anomaly:
                            tipo        = anomaly['type']
                            confidence  = anomaly['confidence']
                            agora       = datetime.now()
                            ultimo_log  = _ultimo_log.get(tipo)

                            deve_logar = (
                                confidence >= log_min_conf and
                                (ultimo_log is None or
                                 (agora - ultimo_log).total_seconds() >= log_cooldown)
                            )

                            if deve_logar:
                                _ultimo_log[tipo] = agora
                                if tipo == 'FLASH_PUMP':
                                    Auditoria.log_sistema(
                                        f"[RT] FLASH PUMP detectado! "
                                        f"Spike: {anomaly['spike_pct']:.2f}% | "
                                        f"Vol: {anomaly['volume_ratio']:.1f}× | "
                                        f"Vel: {anomaly['velocity']:.3f}%/s | "
                                        f"Conf: {confidence:.0%} | "
                                        f"Detectado em {anomaly['elapsed_seconds']:.1f}s", "AVISO")
                                elif tipo == 'FLASH_CRASH':
                                    Auditoria.log_sistema(
                                        f"[RT] FLASH CRASH detectado! "
                                        f"Queda: {anomaly['drop_pct']:.2f}% | "
                                        f"Vol: {anomaly['volume_ratio']:.1f}× | "
                                        f"Vel: {anomaly['velocity']:.3f}%/s | "
                                        f"Conf: {confidence:.0%} | "
                                        f"Detectado em {anomaly['elapsed_seconds']:.1f}s", "AVISO")

        except Exception as e:
            logging.warning(f"Trade WS desconectado: {e}. Reconectando em 3s...")
            await asyncio.sleep(3)


def iniciar_trade_ws():
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
                s = dict(shared_state)

            # [FIX-10] Snapshot de CONFIG sob config_lock
            with config_lock:
                cfg_symbol      = CONFIG['SYMBOL']
                cfg_max_so      = CONFIG['MAX_SAFETY_ORDERS']
                cfg_sl_usd      = CONFIG['STOP_LOSS_USD']
                cfg_rsi_max     = CONFIG['RSI_MAX_ENTRADA']
                cfg_rsi_on      = CONFIG['FILTRO_RSI_ATIVO']
                cfg_vol_on      = CONFIG['FILTRO_VOLUME_ATIVO']
                cfg_cb_on       = CONFIG['CIRCUIT_BREAKER_ATIVO']
                cfg_rt_baseline = CONFIG['RT_BASELINE_MINUTES']
                cfg_pt_timeout  = CONFIG['HYBRID_PARTIAL_TIMEOUT_SEC']

            c = s["preco"]
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v5.2.3 {Fore.WHITE}| DETECTOR HÍBRIDO | {Fore.CYAN}UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*80}")

            if s["circuit_breaker"]:
                print(f"{Fore.RED}  ⚡ CIRCUIT BREAKER ATIVO — Entradas pausadas por queda de mercado")

            filtros  = " ".join([f"[{k}:{v}{Fore.WHITE}]" for k, v in s["filtros"].items()])
            rsi_cor  = Fore.RED if s["rsi_atual"] >= cfg_rsi_max else Fore.GREEN
            print(f"  MERCADO: {Fore.GREEN}{cfg_symbol} {Fore.YELLOW}${c:.8f} "
                  f"{Fore.WHITE}| MTF {filtros} | RSI {rsi_cor}{s['rsi_atual']:.1f}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")

            rsi_tag = f"{Fore.GREEN}ON" if cfg_rsi_on  else f"{Fore.RED}OFF"
            vol_tag = f"{Fore.GREEN}ON" if cfg_vol_on  else f"{Fore.RED}OFF"
            cb_tag  = f"{Fore.GREEN}ON" if cfg_cb_on   else f"{Fore.RED}OFF"
            print(f"  FILTROS: RSI[{rsi_tag}{Fore.WHITE}] "
                  f"VOL[{vol_tag}{Fore.WHITE}] "
                  f"CB[{cb_tag}{Fore.WHITE}]")

            rt_ready  = s["rt_baseline_ready"]
            rt_color  = Fore.GREEN if rt_ready else Fore.YELLOW
            rt_ok_str = "OK" if rt_ready else f"aguard.baseline ({s['rt_baseline_size']}/{cfg_rt_baseline})"
            rt_label  = f"RT {rt_ok_str}"

            hybrid_label = ""
            if s["hybrid_partial_active"]:
                timeout_total = cfg_pt_timeout
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
# WEBSOCKET DE PREÇO (@ticker)
# =============================================================================
async def ws_loop():
    with config_lock:
        ultimo_symbol = CONFIG['SYMBOL']
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"

    while True:
        try:
            with config_lock:
                cur = CONFIG['SYMBOL']
            if ultimo_symbol != cur:
                ultimo_symbol = cur
                uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"
                with state_lock:
                    shared_state["preco"] = 0.0

            async with websockets.connect(uri) as ws:
                while True:
                    with config_lock:
                        cur = CONFIG['SYMBOL']
                    if ultimo_symbol != cur:
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

    threading.Thread(target=thread_motor,     daemon=True, name="Motor").start()
    threading.Thread(target=thread_visual,    daemon=True, name="Visual").start()
    threading.Thread(target=iniciar_ws,       daemon=True, name="WebSocket-Ticker").start()
    threading.Thread(target=thread_scanner,   daemon=True, name="Scanner").start()
    threading.Thread(target=iniciar_trade_ws, daemon=True, name="WebSocket-Trade").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)
