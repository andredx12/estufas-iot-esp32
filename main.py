# Projeto IoT - Estufas do Sr. João (Etapa 4)
# Controle de uma estufa com ESP32 e MicroPython, com Wi-Fi e MQTT

from machine import Pin, ADC, PWM
import dht
import network
import json
import time
from umqtt.simple import MQTTClient

# Cultura da estufa (no projeto do repolho, trocar aqui)
CULTURA = "alface"

# temp_max: ventilar acima disso (°C)
# solo_min: irrigar abaixo disso (%)
LIMITES = {
    "alface": {"temp_max": 24, "solo_min": 60},
    "repolho": {"temp_max": 20, "solo_min": 70},
}
limite = LIMITES[CULTURA]

LUX_MINIMO = 5000  # abaixo disso, ligar a luz

# Folga para desligar, para o relé não ficar ligando e desligando perto do limite
FOLGA_TEMP = 1.0
FOLGA_SOLO = 5.0
FOLGA_LUX = 1000

# Alerta: 5 °C acima do limite ou solo 15 pontos abaixo do mínimo
ALERTA_TEMP = 5
ALERTA_SOLO = 15

INTERVALO = 2  # segundos entre leituras

# Wi-Fi do simulador e broker público do HiveMQ
WIFI_REDE = "Wokwi-GUEST"
WIFI_SENHA = ""
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORTA = 1883

# Tópicos (prefixo para não misturar com outros grupos no broker público)
PREFIXO = "umc/estufas-ack/" + CULTURA
TOPICO_DADOS = PREFIXO + "/telemetria"
TOPICO_STATUS = PREFIXO + "/status"
TOPICO_COMANDO = PREFIXO + "/comando"  # + /irrigacao, /ventilacao ou /iluminacao

# Pinos (sensores analógicos no ADC1, que funciona junto com o Wi-Fi)
PINO_DHT = 33
PINO_LUZ = 32
PINO_SOLO_1 = 35
PINO_SOLO_2 = 34
PINO_RELE_IRRIGACAO = 25
PINO_RELE_VENTILACAO = 26
PINO_RELE_ILUMINACAO = 27
PINO_BUZZER = 13

# Relé acionado em nível alto
LIGA = 1
DESLIGA = 0

# Sensores
sensor_ar = dht.DHT22(Pin(PINO_DHT))
sensor_luz = ADC(Pin(PINO_LUZ), atten=ADC.ATTN_11DB)
sensor_solo_1 = ADC(Pin(PINO_SOLO_1), atten=ADC.ATTN_11DB)
sensor_solo_2 = ADC(Pin(PINO_SOLO_2), atten=ADC.ATTN_11DB)

# Relés desligados na partida
reles = {
    "irrigacao": Pin(PINO_RELE_IRRIGACAO, Pin.OUT, value=DESLIGA),
    "ventilacao": Pin(PINO_RELE_VENTILACAO, Pin.OUT, value=DESLIGA),
    "iluminacao": Pin(PINO_RELE_ILUMINACAO, Pin.OUT, value=DESLIGA),
}

# Buzzer por PWM, sem som na partida
buzzer = PWM(Pin(PINO_BUZZER))
buzzer.freq(2000)
buzzer.duty(0)

# Modo de cada relé: "auto", "ligado" ou "desligado" (os dois últimos pelo painel)
modo = {"irrigacao": "auto", "ventilacao": "auto", "iluminacao": "auto"}
ligado = {"irrigacao": False, "ventilacao": False, "iluminacao": False}
alerta = False

leitura = None
cliente = None
comando_novo = False


# Converter a leitura do ADC (0 a 65535) em porcentagem
def ler_porcentagem(sensor):
    return sensor.read_u16() * 100 / 65535


# Converter a leitura do LDR em lux (fórmula da documentação do Wokwi)
def ler_lux():
    proporcao = sensor_luz.read_u16() / 65535
    if proporcao <= 0:
        return 100000.0
    if proporcao >= 1:
        return 0.0
    resistencia = 10000 * proporcao / (1 - proporcao)
    lux = (50000 * 10 ** 0.7 / resistencia) ** (1 / 0.7)
    return min(lux, 100000.0)


# Ler todos os sensores (None se o DHT22 falhar)
def ler_sensores():
    try:
        sensor_ar.measure()
        temperatura = sensor_ar.temperature()
        umidade_ar = sensor_ar.humidity()
    except OSError:
        print("Falha ao ler o DHT22")
        return None
    s1 = ler_porcentagem(sensor_solo_1)
    s2 = ler_porcentagem(sensor_solo_2)
    return {
        "temperatura": temperatura,
        "umidade_ar": umidade_ar,
        "umidade_solo": (s1 + s2) / 2,
        "solo_1": s1,
        "solo_2": s2,
        "luminosidade": ler_lux(),
    }


# Decisões: ligar ao passar do limite e só desligar depois da folga
def decidir_ventilacao(temperatura, ligada):
    if temperatura > limite["temp_max"]:
        return True
    if temperatura <= limite["temp_max"] - FOLGA_TEMP:
        return False
    return ligada


def decidir_irrigacao(umidade_solo, ligada):
    if umidade_solo < limite["solo_min"]:
        return True
    if umidade_solo >= limite["solo_min"] + FOLGA_SOLO:
        return False
    return ligada


def decidir_iluminacao(lux, ligada):
    if lux < LUX_MINIMO:
        return True
    if lux >= LUX_MINIMO + FOLGA_LUX:
        return False
    return ligada


# Aplicar o modo de cada relé: automático pela leitura, manual pelo painel
def atualizar_saidas():
    global alerta
    if leitura is None:
        return
    automatico = {
        "ventilacao": decidir_ventilacao(leitura["temperatura"], ligado["ventilacao"]),
        "irrigacao": decidir_irrigacao(leitura["umidade_solo"], ligado["irrigacao"]),
        "iluminacao": decidir_iluminacao(leitura["luminosidade"], ligado["iluminacao"]),
    }
    for nome in reles:
        if modo[nome] == "auto":
            ligado[nome] = automatico[nome]
        else:
            ligado[nome] = modo[nome] == "ligado"
        reles[nome].value(LIGA if ligado[nome] else DESLIGA)

    # Buzzer só em condição crítica
    alerta = (leitura["temperatura"] > limite["temp_max"] + ALERTA_TEMP
              or leitura["umidade_solo"] < limite["solo_min"] - ALERTA_SOLO)
    buzzer.duty(512 if alerta else 0)


# Receber comando do painel: LIGAR, DESLIGAR ou AUTO
def receber_comando(topico, mensagem):
    global comando_novo
    nome = topico.decode().split("/")[-1]
    acao = mensagem.decode().strip().upper()
    novos = {"LIGAR": "ligado", "DESLIGAR": "desligado", "AUTO": "auto"}
    if nome not in modo or acao not in novos:
        print("Comando ignorado:", nome, acao)
        return
    modo[nome] = novos[acao]
    comando_novo = True
    print("Comando recebido:", nome, acao)


def conectar_wifi():
    wifi = network.WLAN(network.STA_IF)
    wifi.active(True)
    if wifi.isconnected():
        return True
    print("Conectando ao Wi-Fi", end="")
    wifi.connect(WIFI_REDE, WIFI_SENHA)
    inicio = time.ticks_ms()
    while not wifi.isconnected():
        if time.ticks_diff(time.ticks_ms(), inicio) > 15000:
            print(" falhou")
            return False
        print(".", end="")
        time.sleep(0.2)
    print(" conectado")
    return True


# Conectar ao broker, com last will para marcar "offline" se a conexão cair
def conectar_mqtt():
    global cliente
    try:
        id_cliente = "umc-estufas-ack-" + CULTURA  # fixo: a conexão nova substitui a antiga
        novo = MQTTClient(id_cliente, MQTT_BROKER, port=MQTT_PORTA, keepalive=30)
        novo.set_last_will(TOPICO_STATUS, "offline", retain=True)
        novo.set_callback(receber_comando)
        novo.connect()
        novo.subscribe(TOPICO_COMANDO + "/#")
        novo.publish(TOPICO_STATUS, "online", retain=True)
        cliente = novo
        print("MQTT conectado em", MQTT_BROKER)
    except Exception as erro:
        print("Falha no MQTT:", erro)
        cliente = None


# Enviar leituras e estado dos relés em JSON
def publicar():
    global cliente
    if cliente is None or leitura is None:
        return
    dados = {
        "cultura": CULTURA,
        "temperatura": round(leitura["temperatura"], 1),
        "umidade_ar": round(leitura["umidade_ar"], 1),
        "umidade_solo": round(leitura["umidade_solo"], 1),
        "solo_1": round(leitura["solo_1"], 1),
        "solo_2": round(leitura["solo_2"], 1),
        "luminosidade": round(leitura["luminosidade"]),
        "irrigacao": ligado["irrigacao"],
        "ventilacao": ligado["ventilacao"],
        "iluminacao": ligado["iluminacao"],
        "modo_irrigacao": modo["irrigacao"],
        "modo_ventilacao": modo["ventilacao"],
        "modo_iluminacao": modo["iluminacao"],
        "alerta": alerta,
        "limite_temp": limite["temp_max"],
        "limite_solo": limite["solo_min"],
    }
    try:
        cliente.publish(TOPICO_DADOS, json.dumps(dados))
    except Exception:
        print("Falha ao publicar, conexão MQTT perdida")
        cliente = None


# Mostrar leituras e estado dos relés no monitor serial
def situacao(nome):
    estado = "LIGADA" if ligado[nome] else "DESLIGADA"
    return estado + (" (auto)" if modo[nome] == "auto" else " (manual)")


def mostrar():
    print("=" * 46)
    print("Estufa: " + CULTURA.upper())
    print("Temperatura ....: {:.1f} C (ventila acima de {} C)".format(
        leitura["temperatura"], limite["temp_max"]))
    print("Umidade do ar ..: {:.1f} %".format(leitura["umidade_ar"]))
    print("Umidade do solo : {:.1f} % (sensor 1: {:.0f} %, sensor 2: {:.0f} %)".format(
        leitura["umidade_solo"], leitura["solo_1"], leitura["solo_2"]))
    print("                  irriga abaixo de {} %".format(limite["solo_min"]))
    print("Luminosidade ...: {:.0f} lux (luz abaixo de {} lux)".format(
        leitura["luminosidade"], LUX_MINIMO))
    print("Irrigacao ......: " + situacao("irrigacao"))
    print("Ventilacao .....: " + situacao("ventilacao"))
    print("Iluminacao .....: " + situacao("iluminacao"))
    print("MQTT ...........: " + ("conectado" if cliente else "sem conexao"))
    if alerta:
        print("*** ALERTA: condicao critica na estufa ***")


# Programa principal
print("Controlador da estufa de " + CULTURA + " iniciado")
time.sleep(1)  # esperar o DHT22 estabilizar

if conectar_wifi():
    conectar_mqtt()

proxima_leitura = time.ticks_ms()
ultima_tentativa = time.ticks_ms()

while True:
    agora = time.ticks_ms()

    # Comandos do painel
    if cliente is not None:
        try:
            cliente.check_msg()
        except Exception:
            print("Conexão MQTT perdida")
            cliente = None
    if comando_novo:
        comando_novo = False
        atualizar_saidas()
        publicar()

    # Leitura, controle e envio a cada INTERVALO segundos
    if time.ticks_diff(agora, proxima_leitura) >= 0:
        proxima_leitura = time.ticks_add(agora, INTERVALO * 1000)
        nova = ler_sensores()
        if nova is not None:
            leitura = nova
            atualizar_saidas()
            mostrar()
            publicar()

    # Sem conexão, tentar de novo a cada 10 s sem parar o controle local
    if cliente is None and time.ticks_diff(agora, ultima_tentativa) > 10000:
        ultima_tentativa = agora
        if conectar_wifi():
            conectar_mqtt()

    time.sleep(0.1)
