# =====================================================================================
# SNIPER PHOENIX v4.3.5 - AUDITED EDITION (COM LOGS & CORREÇÕES)
# Features: MTF Entry -> DCA Martingale -> Trailing Stop -> Auto-Save -> Full Audit
# =====================================================================================

import ccxt
import pandas as pd
import time
import configparser
import os
import sys
import threading
from datetime import datetime
from colorama import Fore, Style, init
import signal
import asyncio
import websockets
import json
import logging
import csv

init(autoreset=True)
inicio_bot = datetime.now()

# --- CONFIGURAÇÃO DE ARQUIVOS ---
ARQUIVO_ESTADO = "sniper_state.json"
ARQUIVO_LOG_SISTEMA = "sniper_system.log"
ARQUIVO_LOG_TRADES = "sniper_trades.csv"

# --- SISTEMA DE AUDITORIA (NOVO) ---
class Auditoria:
    @staticmethod
    def configurar():
        # Configura log do sistema (Debug/Info/Erro)
        logging.basicConfig(
            filename=ARQUIVO_LOG_SISTEMA,
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Cria cabeçalho do CSV se não existir
        if not os.path.exists(ARQUIVO_LOG_TRADES):
            with open(ARQUIVO_LOG_TRADES, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["DATA", "PAR", "TIPO", "PRECO", "QTD", "TOTAL_USD", "LUCRO_USD", "LUCRO_PERC", "SALDO_VAULT", "OBS"])

    @staticmethod
    def log_sistema(msg, nivel="INFO"):
        """Registra eventos internos do bot"""
        print_msg = msg # Guarda para exibir no console se necessário
        if nivel == "INFO": logging.info(msg)
        elif nivel == "AVISO": logging.warning(msg)
        elif nivel == "ERRO": logging.error(msg)
        elif nivel == "CRITICO": logging.critical(msg)
        return print_msg

    @staticmethod
    def log_transacao(tipo, preco, qtd, total_usd, lucro_usd=0.0, lucro_perc=0.0, saldo_vault=0.0, obs=""):
        """Registra movimentações financeiras no CSV"""
        try:
            with open(ARQUIVO_LOG_TRADES, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    CONFIG["SYMBOL"],
                    tipo,
                    f"{preco:.8f}",
                    f"{qtd:.8f}",
                    f"{total_usd:.2f}",
                    f"{lucro_usd:.2f}",
                    f"{lucro_perc:.2f}%",
                    f"{saldo_vault:.2f}",
                    obs
                ])
        except Exception as e:
            logging.error(f"Erro ao salvar CSV de transação: {e}")

# --- ESTADO COMPARTILHADO ---
shared_state = {
    "preco": 0.0,
    "erros_consecutivos": 0,  # Contador de erros
    "marcha": "INICIALIZANDO...",
    "status": "Carregando módulos...",
    "mtf": "INICIALIZANDO...",
    "filtros": {},
    "em_operacao": False,
    "preco_medio": 0.0,
    "lucro_perc_atual": 0.0,
    # Variáveis corrigidas para evitar KeyError no visual
    "proxima_compra_p": 0.0,
    "num_safety_orders": 0,
    "alvo_trailing_ativacao": 0.0,
    "stop_atual_trailing": 0.0,
    "trailing_ativo": False,
    "max_p_trailing": 0.0,
    "msg_log": "Sistema Iniciado",
}

menu_ativo = False
exchange = None
ordens_executadas = [] 

# --- CONFIGURAÇÃO GLOBAL ---
CONFIG = {
    "API_KEY": "", 
    "SECRET": "",  
    "SYMBOL": "BTC/USDT",
    "MOEDA_BASE": "USDT", # Adicionado explicitamente
    
    # Gestão de Capital ($120 Teto)
    "CAPITAL_BASE": 14.75,      
    "MAX_SAFETY_ORDERS": 3,     
    
    # Estratégia DCA
    "DCA_VOLUME_SCALE": 1.5,    
    "DCA_STEP_INITIAL": 0.02,   
    "DCA_STEP_SCALE": 1.3,      
    
    # Saída
    "STOP_LOSS": 0.03,          # Adicionado: 3% de SL conforme Padrão de Ouro
    "TRAILING_TRIGGER": 0.015,  
    "TRAILING_DIST": 0.005      
}

# --- PERSISTÊNCIA DE DADOS (CORRIGIDA) ---
def salvar_estado_disco():
    """Salva estado completo incluindo Trailing Stop"""
    try:
        dados = {
            "em_operacao": shared_state["em_operacao"],
            "ordens": ordens_executadas,
            "symbol": CONFIG["SYMBOL"],
            "max_p_trailing": shared_state["max_p_trailing"], # Correção: Salva o topo do trailing
            "trailing_ativo": shared_state["trailing_ativo"],
            "timestamp": str(datetime.now())
        }
        with open(ARQUIVO_ESTADO, "w") as f:
            json.dump(dados, f, indent=4)
        # Auditoria silenciosa para não poluir log
    except Exception as e:
        definir_status(f"Erro crítico ao salvar estado: {e}", "ERRO")

def limpar_estado_disco():
    if os.path.exists(ARQUIVO_ESTADO):
        try:
            os.remove(ARQUIVO_ESTADO)
            Auditoria.log_sistema("Ciclo finalizado com sucesso. Save deletado.", "INFO")
        except: pass

def carregar_estado_disco():
    global ordens_executadas
    if not os.path.exists(ARQUIVO_ESTADO):
        return False
    
    try:
        with open(ARQUIVO_ESTADO, "r") as f:
            dados = json.load(f)
        
        if dados.get("em_operacao") and dados.get("symbol") == CONFIG["SYMBOL"]:
            ordens_executadas = dados["ordens"]
            shared_state["em_operacao"] = True
            shared_state["marcha"] = "RECUPERANDO..."
            
            # Restaura estado do trailing (Correção Vital)
            shared_state["max_p_trailing"] = dados.get("max_p_trailing", 0.0)
            shared_state["trailing_ativo"] = dados.get("trailing_ativo", False)
            
            Auditoria.log_sistema(f"ESTADO RESTAURADO: {len(ordens_executadas)} ordens carregadas.", "AVISO")
            return True
    except Exception as e:
        Auditoria.log_sistema(f"Erro ao ler save: {e}", "ERRO")
    
    return False

# --- CONEXÃO ---
def carregar_configuracoes():
    global exchange, CONFIG
    Auditoria.configurar()
    
    msg_update = ""
    mudanca_symbol = False # Flag para controlar reset
    
    if os.path.exists("config.ini"):
        cp = configparser.ConfigParser()
        cp.read("config.ini")
        try:
            # ... (código de leitura das chaves API igual ao anterior) ...
            CONFIG["API_KEY"] = cp["binance"]["api_key"]
            CONFIG["SECRET"] = cp["binance"]["secret"]
            
            # Lógica de detecção de mudança de par
            novo_symbol = cp["trading"]["symbol"]
            if novo_symbol != CONFIG["SYMBOL"]:
                mudanca_symbol = True
                msg_update += f" [Novo Par: {novo_symbol}]"
            
            CONFIG["SYMBOL"] = novo_symbol
            CONFIG["MOEDA_BASE"] = CONFIG["SYMBOL"].split('/')[1]
            
            # ... (restante das leituras de variaveis igual ao anterior) ...
            old_cap = CONFIG["CAPITAL_BASE"]
            CONFIG["CAPITAL_BASE"] = float(cp["trading"].get("capital_base", CONFIG["CAPITAL_BASE"]))
            
            # ... (restante das configs) ...
            CONFIG["MAX_SAFETY_ORDERS"] = int(cp["trading"].get("max_safety_orders", CONFIG["MAX_SAFETY_ORDERS"]))
            CONFIG["DCA_VOLUME_SCALE"] = float(cp["trading"].get("dca_volume_scale", CONFIG["DCA_VOLUME_SCALE"]))
            CONFIG["DCA_STEP_INITIAL"] = float(cp["trading"].get("dca_step_initial", CONFIG["DCA_STEP_INITIAL"]))
            CONFIG["TRAILING_TRIGGER"] = float(cp["trading"].get("trailing_trigger", CONFIG["TRAILING_TRIGGER"]))
            CONFIG["TRAILING_DIST"] = float(cp["trading"].get("trailing_dist", CONFIG["TRAILING_DIST"]))
            CONFIG["STOP_LOSS"] = float(cp["trading"].get("stop_loss", CONFIG["STOP_LOSS"]))

        except Exception as e: 
            Auditoria.log_sistema(f"Erro ao ler config.ini: {e}", "ERRO")

    # Reconexão ou Atualização
    if exchange is None:
        try:
            print(f"{Fore.YELLOW}Conectando a Binance...")
            exchange = ccxt.binance({
                'apiKey': CONFIG["API_KEY"], 
                'secret': CONFIG["SECRET"], 
                'enableRateLimit': True,
                'options': {'adjustForTimeDifference': True}
            })
            exchange.load_markets()
            definir_status(f"Conectado: {CONFIG['SYMBOL']}", "SUCESSO")
        except Exception as e:
            print(f"Erro Crítico Conexão: {e}")
            sys.exit()
    else:
        # Se houve mudança de Símbolo, reseta filtros e limpa estado visual
        if mudanca_symbol:
            shared_state["filtros"] = {} # Limpa sinais de MTF do par antigo
            shared_state["preco"] = 0.0  # Zera preço até o WS conectar no novo
            shared_state["marcha"] = "AGUARDANDO NOVO PAR"
            
        if msg_update:
            Auditoria.log_sistema(f"Config Atualizada:{msg_update}", "AVISO")
            definir_status(f"Hot Reload:{msg_update}", "SUCESSO")
        else:
            definir_status("Config recarregada (Sem alterações visíveis)", "INFO")

# --- UTILITÁRIOS ---
def definir_status(msg, tipo="INFO"):
    hora = datetime.now().strftime("%H:%M:%S")
    cor = {"SUCESSO": Fore.GREEN, "ERRO": Fore.RED, "AVISO": Fore.YELLOW}.get(tipo, Fore.CYAN)
    shared_state["msg_log"] = f"{Fore.WHITE}[{hora}] {cor}{msg}"
    
    # Integra com a auditoria
    if tipo == "ERRO": Auditoria.log_sistema(msg, "ERRO")
    elif tipo == "SUCESSO": Auditoria.log_sistema(msg, "INFO")

def pânico_sistema(mensagem):
    """Interrompe o bot imediatamente por falha crítica."""
    logging.critical(f"DISJUNTOR ATIVADO: {mensagem}")
    definir_status(f"ERRO CRÍTICO: {mensagem} - DESLIGANDO POR SEGURANÇA", "ERRO")
    
    # Tenta salvar o que for possível antes de fechar
    salvar_estado_disco()
    
    # Encerra o processo
    os._exit(1)

# --- CÉREBRO: ANÁLISE MTF ---
def check_mtf_trend():
    config_tf = {'15m': 25, '1h': 50, '4h': 100} 
    filtros = {}
    votos_positivos = 0
    try:
        for tf, period in config_tf.items():
            ohlcv = exchange.fetch_ohlcv(CONFIG['SYMBOL'], timeframe=tf, limit=period + 5)
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            ema = df['c'].ewm(span=period, adjust=False).mean()
            if df['c'].iloc[-1] > ema.iloc[-1]:
                filtros[tf] = f"{Fore.GREEN}▲"
                votos_positivos += 1
            else:
                filtros[tf] = f"{Fore.RED}▼"
        shared_state["filtros"] = filtros
        return votos_positivos >= 2
    except Exception as e:
        Auditoria.log_sistema(f"Erro MTF: {e}", "ERRO")
        return False

# --- CÁLCULOS ---
def calcular_preco_medio():
    total_vol = sum(o['volume'] for o in ordens_executadas)
    total_custo = sum(o['custo'] for o in ordens_executadas)
    return total_custo / total_vol if total_vol > 0 else 0

def calcular_proxima_safety_order(preco_ult, num_ordem_atual):
    """
    preco_ult: Preço da última ordem executada
    num_ordem_atual: Quantas ordens de segurança JÁ foram feitas (0 para entrada inicial)
    """
    # Se já atingiu o limite de Safety Orders (ex: 3), não calcula a próxima
    if num_ordem_atual >= CONFIG["MAX_SAFETY_ORDERS"]: 
        return None, None
    
    # Pega o custo da última ordem para aplicar a escala de volume
    custo_anterior = ordens_executadas[-1]['custo']
    novo_vol_usd = custo_anterior * CONFIG["DCA_VOLUME_SCALE"]
    
    # Calcula a distância baseada na escala de passos (Step Scale)
    # Ordem 0 (Entrada) -> Próxima é 1: Step Initial * (Scale^0)
    # Ordem 1 -> Próxima é 2: Step Initial * (Scale^1)
    fator_distancia = CONFIG["DCA_STEP_SCALE"] ** num_ordem_atual
    pct_queda = CONFIG["DCA_STEP_INITIAL"] * fator_distancia
    
    novo_preco = preco_ult * (1 - pct_queda)
    
    return novo_preco, novo_vol_usd

# --- VAULT (FUNDING MANAGER) ---
def get_saldo_fundos():
    try:
        bal = exchange.fetch_balance({'type': 'funding'})
        return bal.get(CONFIG["MOEDA_BASE"], {}).get('free', 0)
    except: return 0

def transferir_para_spot(valor):
    try:
        exchange.transfer(CONFIG["MOEDA_BASE"], valor, 'funding', 'spot')
        Auditoria.log_sistema(f"VAULT: Enviado ${valor:.2f} para Spot", "INFO")
        time.sleep(1) 
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Fundos->Spot): {e}", "ERRO")
        return False

def recolher_para_fundos():
    try:
        balance = exchange.fetch_balance()
        saldo_usdt = balance.get(CONFIG["MOEDA_BASE"], {}).get('free', 0)
        if saldo_usdt > 0.5: 
            exchange.transfer(CONFIG["MOEDA_BASE"], saldo_usdt, 'spot', 'funding')
            Auditoria.log_sistema(f"VAULT: Protegido ${saldo_usdt:.2f} em Fundos", "INFO")
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Spot->Fundos): {e}", "ERRO")
        return False

def inicializar_vault():
    definir_status("Auditando Vault...", "INFO")
    recolher_para_fundos()
    try:
        saldo_vault = get_saldo_fundos()
        if saldo_vault < 120:
            definir_status(f"Aviso: Saldo Fundos (${saldo_vault:.2f}) baixo", "AVISO")
        else:
            definir_status(f"Vault pronto! Saldo: ${saldo_vault:.2f}", "SUCESSO")
    except Exception as e:
        definir_status(f"Erro Vault Init: {e}", "ERRO")

# --- EXECUÇÃO ---
def comprar_com_vault(valor_usd, motivo="Entrada"):
    try:
        if transferir_para_spot(valor_usd):
            # Tenta executar a ordem
            ordem = exchange.create_order(
                CONFIG['SYMBOL'], 
                'market', 
                'buy', 
                None, 
                params={'quoteOrderQty': exchange.cost_to_precision(CONFIG['SYMBOL'], valor_usd)}
            )
            
            # Resetamos o contador de erros após uma operação bem-sucedida da API
            shared_state["erros_consecutivos"] = 0 
            
            res = {
                'ok': True, 
                'p': float(ordem['price'] if ordem['price'] else ordem['average']), 
                'v': float(ordem['amount']), 
                'c': float(ordem['cost'])
            }
            # Grava a compra no CSV
            Auditoria.log_transacao("COMPRA", res['p'], res['v'], res['c'], obs=motivo)
            return res

    except Exception as e:
        # AQUI É ONDE O CONTADOR DEVE AGIR
        shared_state["erros_consecutivos"] += 1
        logging.error(f"Erro Compra Market Binance: {e} | Tentativa: {shared_state['erros_consecutivos']}/5")
        
        # Se falhou, a variável 'ordem' não existe, então retornamos erro antes de dar o crash
        return {'ok': False, 'msg': str(e)}
    
    return {'ok': False, 'msg': 'Falha desconhecida na transferência'}

def executar_venda_mercado(qtd, motivo="Saida"):
    try:
        # 1. Carrega mercados se necessário para garantir precisão
        if not exchange.markets: exchange.load_markets()
        
        # 2. Formata a quantidade para o padrão da moeda (ex: PEPE sem decimais)
        qtd_formatada = exchange.amount_to_precision(CONFIG['SYMBOL'], qtd)
        
        # 3. Executa a ordem com a quantidade corrigida
        ordem = exchange.create_order(CONFIG['SYMBOL'], 'market', 'sell', qtd_formatada)
        
        # 4. Dados do resultado
        p = float(ordem['avgPrice'] if 'avgPrice' in ordem else ordem.get('price', 0))
        c = float(ordem['cummulativeQuoteQty']) if 'cummulativeQuoteQty' in ordem else float(ordem['cost'])
        
        return {'ok': True, 'p': p, 'c': c}
    except Exception as e:
        Auditoria.log_sistema(f"Erro Venda ({motivo}): {e}", "ERRO")
        return {'ok': False, 'msg': str(e)}

# --- LOOP DCA ---
def loop_dca_ativo():
    shared_state["em_operacao"] = True
    shared_state["marcha"] = "DCA ATIVO"
    
    trailing_ativo = shared_state.get("trailing_ativo", False)
    max_p = shared_state.get("max_p_trailing", 0.0)
    
    # Recalcula próxima ordem baseada na última executada
    prox_p, prox_v_usd = calcular_proxima_safety_order(ordens_executadas[-1]['p'], len(ordens_executadas)-1)
    
    while True:
        # VERIFICAÇÃO DE SEGURANÇA (DISJUNTOR)
        if shared_state.get("erros_consecutivos", 0) >= 5:
            pânico_sistema("Múltiplos erros de execução detectados. Interrompendo para poupar o Vault.")
            break
        try:
            if not shared_state["em_operacao"]: break
            preco = shared_state["preco"]
            if preco <= 0: 
                time.sleep(0.5)
                continue
            
            pm = calcular_preco_medio()
            lucro_atual_perc = ((preco/pm)-1)*100
            
            # Atualiza visualizador
            shared_state["preco_medio"] = pm
            shared_state["lucro_perc_atual"] = lucro_atual_perc
            shared_state["num_safety_orders"] = len(ordens_executadas) - 1
            shared_state["proxima_compra_p"] = prox_p if prox_p else 0.0

            # 1. GESTÃO DE COMPRA (DCA)
            if prox_p and preco <= prox_p:
                msg_ordem = f"DCA #{len(ordens_executadas)}"
                definir_status(f"Executando {msg_ordem}...", "AVISO")
                
                # A função comprar_com_vault JÁ faz o log de "COMPRA" internamente agora
                res = comprar_com_vault(prox_v_usd, motivo=msg_ordem)
                
                if res['ok']: # Usa 'res', não 'res_venda'
                    # Adiciona a ordem à memória
                    ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                    
                    # Reseta gatilhos do trailing
                    trailing_ativo = False 
                    max_p = 0.0
                    
                    # Salva e recalcula o próximo passo
                    salvar_estado_disco()
                    prox_p, prox_v_usd = calcular_proxima_safety_order(res['p'], len(ordens_executadas)-1)
                else:
                    definir_status(f"Falha DCA: {res.get('msg', 'Erro desconhecido')}", "ERRO")            

            # 2. PROTEÇÃO: STOP LOSS PÓS-DCA (PADRÃO DE OURO)
            # Verifica se já usamos o máximo de ordens (ex: 3)
            if (len(ordens_executadas) - 1) >= CONFIG["MAX_SAFETY_ORDERS"]:
                # Se o prejuízo atingir o SL configurado (ex: 3%)
                if lucro_atual_perc <= -(CONFIG["STOP_LOSS"] * 100):
                    definir_status(f"STOP LOSS FINAL ATINGIDO: {lucro_atual_perc:.2f}%", "ERRO")
                    qtd_total = sum(o['volume'] for o in ordens_executadas)
                    res_venda = executar_venda_mercado(qtd_total, motivo="SL Pós-DCA")
                    
                    if res_venda['ok']:
                        Auditoria.log_transacao("STOP LOSS", res_venda['p'], qtd_total, res_venda['c'], 
                                               lucro_usd=(res_venda['c'] - sum(o['custo'] for o in ordens_executadas)),
                                               lucro_perc=lucro_atual_perc, obs="Ejeção Final")
                        finalizar_ciclo_sucesso()
                        break

            # 3. GESTÃO DE SAÍDA (TRAILING STOP)
            gatilho_ts = pm * (1 + CONFIG["TRAILING_TRIGGER"])
            shared_state["alvo_trailing_ativacao"] = gatilho_ts
            
            if not trailing_ativo:
                if preco >= gatilho_ts:
                    trailing_ativo = True
                    max_p = preco
                    shared_state["trailing_ativo"] = True
                    definir_status("Trailing Ativado!", "SUCESSO")
            else:
                if preco > max_p: max_p = preco
                stop_price = max_p * (1 - CONFIG["TRAILING_DIST"])
                shared_state["stop_atual_trailing"] = stop_price
                
                if preco <= stop_price:
                    definir_status("Take Profit atingido no Trailing!", "SUCESSO")
                    qtd_total = sum(o['volume'] for o in ordens_executadas)
                    res_venda = executar_venda_mercado(qtd_total, motivo="TP Trailing")

                    if res_venda['ok']:
                        custo_compra = sum(o['custo'] for o in ordens_executadas)
                        Auditoria.log_transacao(
                            tipo="TAKE PROFIT",
                            preco=res_venda['p'],
                            qtd=qtd_total,
                            total_usd=res_venda['c'],
                            lucro_usd=(res_venda['c'] - custo_compra),
                            lucro_perc=lucro_atual_perc,
                            saldo_vault=get_saldo_fundos(),
                            obs="Venda via Trailing Stop"
                        )
                        finalizar_ciclo_sucesso()
                        break
            time.sleep(0.5)

        except Exception as e:
            definir_status(f"Erro Loop: {e}", "ERRO")
            time.sleep(2)

def finalizar_ciclo_sucesso():
    recolher_para_fundos()
    ordens_executadas.clear()
    limpar_estado_disco()
    shared_state["em_operacao"] = False
    shared_state["trailing_ativo"] = False
    shared_state["alvo_trailing_ativacao"] = 0


# --- THREAD MOTOR ---
def thread_motor():
    if carregar_estado_disco():
        shared_state["mtf"] = "RETOMADO"
        loop_dca_ativo()
    else:
        inicializar_vault()

    while True:
        try:
            if not shared_state["em_operacao"]:
                sinal_atual = shared_state["mtf"]
                shared_state["marcha"] = "ESPERANDO MTF"
                # Limpa variaveis visuais antigas
                shared_state["preco_medio"] = 0
                shared_state["proxima_compra_p"] = 0
            
                if check_mtf_trend() and shared_state["preco"] > 0:
                    definir_status("Sinal MTF Confirmado! Iniciando...", "SUCESSO")
                    res = comprar_com_vault(CONFIG["CAPITAL_BASE"], motivo="Entrada MTF")
                    if res['ok']:
                        ordens_executadas.append({'p': res['p'], 'volume': res['v'], 'custo': res['c']})
                        salvar_estado_disco()
                        loop_dca_ativo()
            time.sleep(1)
            # Se a execução chegar aqui sem erro, resetamos o contador
            shared_state["erros_consecutivos"] = 0

        except Exception as e:
            shared_state["erros_consecutivos"] += 1
            erro_msg = f"Erro no Motor ({shared_state['erros_consecutivos']}/5): {e}"
            logging.error(erro_msg)
            
            if shared_state["erros_consecutivos"] >= 5:
                pânico_sistema("Limite de 5 erros consecutivos atingido.")
            
            time.sleep(5) # Espera um pouco mais em caso de erro

# --- THREAD VISUAL ---
def thread_visual():
    while True:
        if not menu_ativo:
            os.system('cls' if os.name == 'nt' else 'clear')
            uptime = str(datetime.now() - inicio_bot).split('.')[0]

            s = shared_state
            c = s["preco"]
            
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v4.3.5 {Fore.WHITE}| AUDITED EDITION | {Fore.CYAN}UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*64}")
            
            filtros = " ".join([f"[{k}:{v}{Fore.WHITE}]" for k, v in s["filtros"].items()])
            print(f"  MERCADO: {Fore.GREEN}{CONFIG['SYMBOL']} {Fore.YELLOW}${c:.8f} {Fore.WHITE}| MTF {filtros}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")
            
            if s["em_operacao"]:
                print(f"{Fore.YELLOW}{'-'*64}")
                pnl = s['lucro_perc_atual']
                cor_pnl = Fore.GREEN if pnl > 0 else Fore.RED
                print(f" P. MÉDIO: {Fore.WHITE}${s['preco_medio']:.8f} {cor_pnl}{pnl:.2f}%")
                
                # Safety Orders
                usadas = s['num_safety_orders']
                total = CONFIG['MAX_SAFETY_ORDERS']
                barra = "▰" * usadas + "▱" * (total - usadas)
                print(f"   SAFETY: {Fore.YELLOW}{barra} ({usadas}/{total})")
                
                # Próxima Compra
                if s['proxima_compra_p'] > 0:
                    dist_dca = ((s['proxima_compra_p'] / c) - 1) * 100 if c > 0 else 0
                    print(f" DCA PROX: {Fore.RED}${s['proxima_compra_p']:.8f} ({dist_dca:.2f}%)")
                
                # Trailing
                if s['trailing_ativo']:
                     dist_venda = ((c / s['stop_atual_trailing']) - 1) * 100 if s['stop_atual_trailing'] > 0 else 0
                     print(f" TRAILING: {Fore.MAGENTA}ATIVO (Stop: ${s['stop_atual_trailing']:.8f} | Recuo: {dist_venda:.2f}%)")
                elif s['alvo_trailing_ativacao'] > 0:
                     dist_alvo = ((s['alvo_trailing_ativacao'] / c) - 1) * 100 if c > 0 else 0
                     print(f"  ALVO TS: {Fore.CYAN}${s['alvo_trailing_ativacao']:.8f} (+{dist_alvo:.2f}%)")
            
            print(f"{Fore.YELLOW}{'='*64}")
            print(f"      LOG: {s['msg_log']}")
            print(f"{Fore.YELLOW}{'='*64}")
            print(f" {Fore.WHITE}[Ctrl+C] MENU | LOGS SALVOS EM DISCO")

        time.sleep(5)

# --- SCANNER MTF ---
def thread_scanner():
    while True:
        try:
            # check_mtf_trend já atualiza shared_state["filtros"] internamente
            sinal_bool = check_mtf_trend() 
            
            # Precisamos converter o booleano em texto para o shared_state["mtf"]
            shared_state["mtf"] = "COMPRA" if sinal_bool else "AGUARDANDO"
            
            time.sleep(30) 
        except Exception as e:
            logging.error(f"Erro no Scanner MTF: {e}")
            time.sleep(10)

# --- MENU ---
def acionar_menu(signum, frame):
    """ Chamada pelo Ctrl+C. Pausa o visual e abre opções. """
    global menu_ativo
    menu_ativo = True
    
    # Limpa tela para mostrar o menu
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print(f"{Fore.MAGENTA}╔══════════════════════════════════════╗")
    print(f"{Fore.MAGENTA}║      MENU DE CONTROLE SNIPER         ║")
    print(f"{Fore.MAGENTA}╠══════════════════════════════════════╣")
    print(f"{Fore.WHITE}║ 1. VOLTAR AO MONITORAMENTO           ║")
    print(f"{Fore.YELLOW}║ 2. ATUALIZAR CONFIG.INI (HOT RELOAD) ║")
    print(f"{Fore.RED}║ 3. ENCERRAR (DESLIGAR BOT)           ║")
    print(f"{Fore.MAGENTA}╚══════════════════════════════════════╝")
    
    try:
        opt = input(f"\n{Fore.CYAN}➤ Escolha uma opção [1-3]: {Fore.WHITE}")
        
        if opt == '1':
            print(f"{Fore.GREEN}Retornando ao dashboard...")
            time.sleep(0.5)
            
        elif opt == '2':
            print(f"{Fore.YELLOW}Lendo arquivo config.ini...")
            carregar_configuracoes() # Chama a função melhorada acima
            print(f"{Fore.GREEN}Configurações aplicadas com sucesso!")
            time.sleep(2) # Pausa maior para ler o log
            
        elif opt == '3':
            print(f"{Fore.RED}Salvando dados e encerrando...")
            salvar_estado_disco() # Garante save antes de sair
            os._exit(0)
            
        else:
            print(f"{Fore.RED}Opção inválida!")
            time.sleep(1)

    except Exception as e:
        print(f"Erro no menu: {e}")
    
    # Libera o menu para a thread visual retomar
    menu_ativo = False

async def ws_loop():
    # Guarda o símbolo atual para comparar depois
    ultimo_symbol = CONFIG['SYMBOL']
    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/','').lower()}@ticker"
    
    while True:
        try:
            # Verifica se o símbolo mudou nas configurações
            if ultimo_symbol != CONFIG['SYMBOL']:
                print(f"\n{Fore.MAGENTA}[WS] Detectada mudança de par: {ultimo_symbol} -> {CONFIG['SYMBOL']}")
                ultimo_symbol = CONFIG['SYMBOL']
                uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/','').lower()}@ticker"
                shared_state["preco"] = 0.0 # Reseta preço visualmente
                
            async with websockets.connect(uri) as ws:
                while True:
                    # Se mudou o símbolo durante a conexão, força desconexão para recriar
                    if ultimo_symbol != CONFIG['SYMBOL']:
                        break 
                        
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        dados = json.loads(msg)
                        shared_state["preco"] = float(dados['c'])
                    except asyncio.TimeoutError:
                        # Timeout serve para dar chance do loop verificar se o Symbol mudou
                        continue 
        except Exception as e:
            # Se der erro de conexão, espera um pouco e tenta de novo
            await asyncio.sleep(2)

def iniciar_ws():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_loop())

if __name__ == "__main__":
    signal.signal(signal.SIGINT, acionar_menu)
    carregar_configuracoes()
    threading.Thread(target=thread_motor, daemon=True).start()
    threading.Thread(target=thread_visual, daemon=True).start()
    threading.Thread(target=iniciar_ws, daemon=True).start()
    threading.Thread(target=thread_scanner, daemon=True).start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: sys.exit()