# Projeto IoT - Estufas do Sr. João (Etapa 3)
# Controle de uma estufa com ESP32 e MicroPython

from machine import Pin, ADC, PWM
import dht
import time

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
rele_irrigacao = Pin(PINO_RELE_IRRIGACAO, Pin.OUT, value=DESLIGA)
rele_ventilacao = Pin(PINO_RELE_VENTILACAO, Pin.OUT, value=DESLIGA)
rele_iluminacao = Pin(PINO_RELE_ILUMINACAO, Pin.OUT, value=DESLIGA)

# Buzzer por PWM, sem som na partida
buzzer = PWM(Pin(PINO_BUZZER))
buzzer.freq(2000)
buzzer.duty(0)

irrigando = False
ventilando = False
iluminando = False


# Converter a leitura do ADC (0 a 65535) em porcentagem
def ler_porcentagem(sensor):
    return sensor.read_u16() * 100 / 65535


# Umidade do solo: média dos dois sensores
def ler_umidade_solo():
    s1 = ler_porcentagem(sensor_solo_1)
    s2 = ler_porcentagem(sensor_solo_2)
    return (s1 + s2) / 2, s1, s2


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


def acionar(rele, ligado):
    rele.value(LIGA if ligado else DESLIGA)


# Mostrar leituras e estado dos relés no monitor serial
def situacao(ligado):
    return "LIGADA" if ligado else "DESLIGADA"


def mostrar(temperatura, umidade_ar, solo, s1, s2, lux, critico):
    print("=" * 46)
    print("Estufa: " + CULTURA.upper())
    print("Temperatura ....: {:.1f} C (ventila acima de {} C)".format(
        temperatura, limite["temp_max"]))
    print("Umidade do ar ..: {:.1f} %".format(umidade_ar))
    print("Umidade do solo : {:.1f} % (sensor 1: {:.0f} %, sensor 2: {:.0f} %)".format(
        solo, s1, s2))
    print("                  irriga abaixo de {} %".format(limite["solo_min"]))
    print("Luminosidade ...: {:.0f} lux (luz abaixo de {} lux)".format(lux, LUX_MINIMO))
    print("Irrigacao ......: " + situacao(irrigando))
    print("Ventilacao .....: " + situacao(ventilando))
    print("Iluminacao .....: " + situacao(iluminando))
    if critico:
        print("*** ALERTA: condicao critica na estufa ***")


# Programa principal
print("Controlador da estufa de " + CULTURA + " iniciado")
time.sleep(1)  # esperar o DHT22 estabilizar

while True:
    try:
        sensor_ar.measure()
        temperatura = sensor_ar.temperature()
        umidade_ar = sensor_ar.humidity()
    except OSError:
        print("Falha ao ler o DHT22, nova tentativa em {} s".format(INTERVALO))
        time.sleep(INTERVALO)
        continue

    solo, s1, s2 = ler_umidade_solo()
    lux = ler_lux()

    ventilando = decidir_ventilacao(temperatura, ventilando)
    irrigando = decidir_irrigacao(solo, irrigando)
    iluminando = decidir_iluminacao(lux, iluminando)

    acionar(rele_ventilacao, ventilando)
    acionar(rele_irrigacao, irrigando)
    acionar(rele_iluminacao, iluminando)

    # Buzzer só em condição crítica
    critico = (temperatura > limite["temp_max"] + ALERTA_TEMP
               or solo < limite["solo_min"] - ALERTA_SOLO)
    buzzer.duty(512 if critico else 0)

    mostrar(temperatura, umidade_ar, solo, s1, s2, lux, critico)
    time.sleep(INTERVALO)
