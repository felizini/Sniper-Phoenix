#!/usr/bin/env python3
"""
=============================================================================
DETECTOR DE ANOMALIAS EM TEMPO REAL (TICK-BY-TICK)
=============================================================================
Versão que detecta flash pumps/crashes DURANTE a vela, não depois!

DIFERENÇAS vs DETECTOR ORIGINAL:
- Original: Analisa velas fechadas (ex: detecta às 16:01 um crash de 16:00)
- Tempo Real: Monitora preço tick-by-tick (detecta em segundos)

ARQUITETURA:
1. WebSocket recebe ticks de preço (1-10× por segundo)
2. Calcula "vela parcial" em tempo real
3. Detecta anomalia ENQUANTO acontece
4. Age imediatamente (não espera vela fechar)
=============================================================================
"""

import asyncio
import websockets
import json
import time
from datetime import datetime, timedelta
from collections import deque
import numpy as np
import logging

logging.basicConfig(level=logging.INFO)


class RealtimeAnomalyDetector:
    """
    Detector de anomalias baseado em stream de ticks (tempo real).
    
    Em vez de esperar velas fecharem, monitora preço tick-a-tick
    e detecta anomalias em segundos.
    """
    
    def __init__(self, symbol="PAXGUSDT", baseline_minutes=60):
        self.symbol = symbol
        self.baseline_minutes = baseline_minutes
        
        # Configuração de thresholds (ajustável)
        self.PRICE_SPIKE_THRESHOLD = 0.003   # 0.3% (spike mínimo)
        self.VOLUME_SPIKE_THRESHOLD = 3.0    # 3× média
        self.VELOCITY_THRESHOLD = 0.001      # 0.1%/segundo (velocidade)
        
        # Janela de detecção em tempo real (segundos)
        self.DETECTION_WINDOW = 30  # 30 segundos
        
        # Históricos de baseline (minutos fechados)
        self.baseline_highs = deque(maxlen=baseline_minutes)
        self.baseline_lows = deque(maxlen=baseline_minutes)
        self.baseline_volumes = deque(maxlen=baseline_minutes)
        
        # Estado da "vela parcial" atual (tempo real)
        self.current_minute_start = None
        self.current_minute_open = None
        self.current_minute_high = None
        self.current_minute_low = None
        self.current_minute_close = None
        self.current_minute_volume = 0.0
        self.current_minute_ticks = 0
        
        # Buffer de ticks recentes (últimos 30 segundos)
        self.recent_ticks = deque(maxlen=300)  # 10 ticks/seg × 30seg
        
        # Preço anterior (para calcular velocidade)
        self.last_price = None
        self.last_tick_time = None
        
        # Estatísticas
        self.flash_pumps_detected = 0
        self.flash_crashes_detected = 0
        
        # Flags
        self.baseline_ready = False
        self.ENABLED = True
    
    def update_tick(self, price, volume, timestamp=None):
        """
        Processa um tick de preço em tempo real.
        
        Args:
            price: Preço atual
            volume: Volume do tick (opcional)
            timestamp: Timestamp do tick
        """
        if timestamp is None:
            timestamp = datetime.now()
        
        price = float(price)
        volume = float(volume) if volume else 0
        
        # 1. Inicializa minuto se necessário
        current_minute = timestamp.replace(second=0, microsecond=0)
        
        if self.current_minute_start != current_minute:
            # Minuto mudou - finaliza anterior e inicia novo
            self._finalize_minute()
            self._start_new_minute(current_minute, price)
        
        # 2. Atualiza vela parcial
        if self.current_minute_high is None or price > self.current_minute_high:
            self.current_minute_high = price
        if self.current_minute_low is None or price < self.current_minute_low:
            self.current_minute_low = price
        
        self.current_minute_close = price
        self.current_minute_volume += volume
        self.current_minute_ticks += 1
        
        # 3. Adiciona ao buffer de ticks recentes
        self.recent_ticks.append({
            'price': price,
            'volume': volume,
            'timestamp': timestamp
        })
        
        # 4. Calcula velocidade de mudança
        velocity = 0
        if self.last_price and self.last_tick_time:
            time_diff = (timestamp - self.last_tick_time).total_seconds()
            if time_diff > 0:
                price_change_pct = ((price / self.last_price) - 1)
                velocity = price_change_pct / time_diff  # % por segundo
        
        self.last_price = price
        self.last_tick_time = timestamp
        
        # 5. DETECTA ANOMALIA EM TEMPO REAL
        if self.baseline_ready:
            anomaly = self._detect_realtime_anomaly(velocity)
            if anomaly:
                return anomaly
        
        return None
    
    def _start_new_minute(self, minute_start, open_price):
        """Inicia uma nova vela parcial."""
        self.current_minute_start = minute_start
        self.current_minute_open = open_price
        self.current_minute_high = open_price
        self.current_minute_low = open_price
        self.current_minute_close = open_price
        self.current_minute_volume = 0.0
        self.current_minute_ticks = 0
    
    def _finalize_minute(self):
        """Finaliza minuto anterior e adiciona ao baseline."""
        if self.current_minute_start is None:
            return
        
        # Adiciona ao baseline
        if self.current_minute_high:
            self.baseline_highs.append(self.current_minute_high)
        if self.current_minute_low:
            self.baseline_lows.append(self.current_minute_low)
        if self.current_minute_volume > 0:
            self.baseline_volumes.append(self.current_minute_volume)
        
        # Marca baseline como pronto após N minutos
        if len(self.baseline_highs) >= self.baseline_minutes:
            self.baseline_ready = True
    
    def _detect_realtime_anomaly(self, velocity):
        """
        Detecta anomalia em TEMPO REAL baseada em:
        1. Spike do high/low vs baseline
        2. Volume acumulado no minuto vs média
        3. Velocidade de mudança (% por segundo)
        """
        if not self.baseline_ready:
            return None
        
        # 1. Calcula spike atual vs baseline
        avg_high = np.mean(self.baseline_highs)
        avg_low = np.mean(self.baseline_lows)
        avg_volume = np.mean(self.baseline_volumes)
        
        # Spike de alta (pump)
        if self.current_minute_high:
            pump_spike = ((self.current_minute_high / avg_high) - 1)
        else:
            pump_spike = 0
        
        # Spike de baixa (crash)
        if self.current_minute_low:
            crash_drop = ((avg_low / self.current_minute_low) - 1)
        else:
            crash_drop = 0
        
        # Volume spike
        volume_ratio = self.current_minute_volume / avg_volume if avg_volume > 0 else 0
        
        # 2. Velocidade extrema (mudança muito rápida)
        velocity_extreme = abs(velocity) >= self.VELOCITY_THRESHOLD
        
        # 3. Detecta FLASH CRASH em tempo real
        if crash_drop >= self.PRICE_SPIKE_THRESHOLD:
            if volume_ratio >= self.VOLUME_SPIKE_THRESHOLD or velocity_extreme:
                # Calcula confiança
                confidence = min(
                    crash_drop / 0.01,
                    volume_ratio / 5,
                    abs(velocity) / self.VELOCITY_THRESHOLD,
                    1.0
                )
                
                self.flash_crashes_detected += 1
                
                return {
                    "type": "FLASH_CRASH",
                    "drop_pct": crash_drop * 100,
                    "volume_ratio": volume_ratio,
                    "velocity": velocity * 100,  # % por segundo
                    "confidence": confidence,
                    "price_bottom": self.current_minute_low,
                    "detection_type": "REALTIME",
                    "elapsed_seconds": (datetime.now() - self.current_minute_start).total_seconds()
                }
        
        # 4. Detecta FLASH PUMP em tempo real
        if pump_spike >= self.PRICE_SPIKE_THRESHOLD:
            if volume_ratio >= self.VOLUME_SPIKE_THRESHOLD or velocity_extreme:
                confidence = min(
                    pump_spike / 0.01,
                    volume_ratio / 5,
                    abs(velocity) / self.VELOCITY_THRESHOLD,
                    1.0
                )
                
                self.flash_pumps_detected += 1
                
                return {
                    "type": "FLASH_PUMP",
                    "spike_pct": pump_spike * 100,
                    "volume_ratio": volume_ratio,
                    "velocity": velocity * 100,
                    "confidence": confidence,
                    "price_peak": self.current_minute_high,
                    "detection_type": "REALTIME",
                    "elapsed_seconds": (datetime.now() - self.current_minute_start).total_seconds()
                }
        
        return None
    
    def get_stats(self):
        """Retorna estatísticas do detector."""
        return {
            "baseline_ready": self.baseline_ready,
            "baseline_size": len(self.baseline_highs),
            "flash_pumps": self.flash_pumps_detected,
            "flash_crashes": self.flash_crashes_detected,
            "current_price": self.current_minute_close,
            "current_high": self.current_minute_high,
            "current_low": self.current_minute_low,
        }


# =============================================================================
# INTEGRAÇÃO COM WEBSOCKET BINANCE
# =============================================================================

async def binance_websocket_stream(symbol, detector):
    """
    Conecta ao WebSocket da Binance e alimenta o detector em tempo real.
    
    Args:
        symbol: Ex: "paxgusdt"
        detector: Instância do RealtimeAnomalyDetector
    """
    uri = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"
    
    logging.info(f"Conectando ao WebSocket: {symbol}")
    
    try:
        async with websockets.connect(uri) as websocket:
            logging.info(f"✅ Conectado! Aguardando ticks...")
            
            async for message in websocket:
                data = json.loads(message)
                
                # Parse do tick
                price = float(data['p'])
                quantity = float(data['q'])
                timestamp = datetime.fromtimestamp(data['T'] / 1000)
                
                # Alimenta detector
                anomaly = detector.update_tick(price, quantity, timestamp)
                
                # Se detectou anomalia, age!
                if anomaly:
                    logging.warning(f"\n{'='*80}")
                    logging.warning(f"🚨 {anomaly['type']} DETECTADO EM TEMPO REAL!")
                    logging.warning(f"{'='*80}")
                    logging.warning(f"Tipo: {anomaly['type']}")
                    
                    if anomaly['type'] == 'FLASH_CRASH':
                        logging.warning(f"Queda: {anomaly['drop_pct']:.2f}%")
                        logging.warning(f"Fundo: ${anomaly['price_bottom']:.2f}")
                    else:
                        logging.warning(f"Spike: {anomaly['spike_pct']:.2f}%")
                        logging.warning(f"Pico: ${anomaly['price_peak']:.2f}")
                    
                    logging.warning(f"Volume: {anomaly['volume_ratio']:.1f}× média")
                    logging.warning(f"Velocidade: {anomaly['velocity']:.3f}%/seg")
                    logging.warning(f"Confiança: {anomaly['confidence']:.0%}")
                    logging.warning(f"Detectado em: {anomaly['elapsed_seconds']:.1f}s do início da vela")
                    logging.warning(f"{'='*80}\n")
                    
                    # AQUI: Chame sua função de venda/compra de emergência
                    # executar_ordem_emergencia(anomaly)
    
    except Exception as e:
        logging.error(f"Erro no WebSocket: {e}")


# =============================================================================
# EXEMPLO DE USO
# =============================================================================

async def main():
    """Exemplo: Monitora PAXG em tempo real."""
    
    # Inicializa detector
    detector = RealtimeAnomalyDetector(
        symbol="PAXGUSDT",
        baseline_minutes=60  # 1 hora de baseline
    )
    
    logging.info("="*80)
    logging.info("DETECTOR DE ANOMALIAS EM TEMPO REAL")
    logging.info("="*80)
    logging.info(f"Símbolo: {detector.symbol}")
    logging.info(f"Baseline: {detector.baseline_minutes} minutos")
    logging.info(f"Thresholds:")
    logging.info(f"  - Spike: {detector.PRICE_SPIKE_THRESHOLD*100:.2f}%")
    logging.info(f"  - Volume: {detector.VOLUME_SPIKE_THRESHOLD:.1f}×")
    logging.info(f"  - Velocidade: {detector.VELOCITY_THRESHOLD*100:.3f}%/seg")
    logging.info("="*80)
    logging.info("Aguardando baseline (60 minutos)...")
    logging.info("="*80 + "\n")
    
    # Inicia stream
    await binance_websocket_stream("paxgusdt", detector)


if __name__ == "__main__":
    asyncio.run(main())
