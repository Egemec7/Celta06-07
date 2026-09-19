import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import serial


# Ajuste estas duas constantes conforme o adaptador instalado no carro.
PORTA = "/dev/ttyUSB0"
BAUDRATE = 38400

# Constantes do Celta 1.0 VHC Flexpower (X10YFL)
DISPLACEMENT = 0.999
VE = 0.80
AFR = 9.0
DENSIDADE = 789.0


class LeitorECU(threading.Thread):
    """Lê a ECU sem bloquear a interface gráfica."""

    def __init__(self, porta, dados, erros, parar):
        super().__init__(daemon=True)
        self.porta = porta
        self.dados = dados
        self.erros = erros
        self.parar = parar
        self.ser = None

    def cmd(self, comando, delay=0.1):
        self.ser.reset_input_buffer()
        self.ser.write((comando + "\r\n").encode("ascii"))
        time.sleep(delay)
        resposta = self.ser.read_until(b">").decode("ascii", errors="ignore")
        return resposta.replace(">", " ").replace("\r", " ").replace("\n", " ").strip()

    @staticmethod
    def interpretar(resposta):
        tokens = resposta.upper().split()
        for i in range(len(tokens) - 1):
            if tokens[i] == "61" and tokens[i + 1] == "01":
                hex_bytes = [
                    valor for valor in tokens[i + 2:]
                    if len(valor) == 2 and all(c in "0123456789ABCDEF" for c in valor)
                ]
                if len(hex_bytes) < 82:
                    return None

                b = [int(valor, 16) for valor in hex_bytes]
                tensao_bat = b[31] * 0.1
                rpm = ((b[32] << 8) | b[33]) / 4.0
                temp_ect = b[38] - 40
                temp_iat = b[40] - 40
                tps = b[60] * 100.0 / 255.0
                map_kpa = float(b[80])

                t_kelvin = max(temp_iat + 273.15, 1.0)
                maf_gps = (
                    rpm * map_kpa * DISPLACEMENT * VE
                    / (120.0 * 287.05 * t_kelvin) * 1000.0
                )
                consumo_lh = (
                    maf_gps * 3600.0 / (AFR * DENSIDADE)
                    if rpm > 350 else 0.0
                )
                return {
                    "rpm": rpm,
                    "map": map_kpa,
                    "tps": tps,
                    "ect": temp_ect,
                    "iat": temp_iat,
                    "bat": tensao_bat,
                    "consumo": consumo_lh,
                }
        return None

    def run(self):
        try:
            self.ser = serial.Serial(self.porta, BAUDRATE, timeout=1.0)
            self.cmd("ATZ", 0.5)
            self.cmd("ATE0")
            self.cmd("ATL0")
            self.cmd("ATSP5")
            self.cmd("ATSH 81 11 F1")
            self.cmd("ATFI", 0.5)

            while not self.parar.is_set():
                valores = self.interpretar(self.cmd("21 01", 0.06))
                if valores:
                    self.dados.put(valores)
                time.sleep(0.04)
        except Exception as exc:
            self.erros.put(str(exc))
        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()


class Gauge(tk.Canvas):
    """Medidor semicircular desenhado apenas com Canvas do Tkinter."""

    def __init__(self, parent, titulo, unidade, minimo, maximo, cor="#35d0ba", **kwargs):
        super().__init__(parent, bg="#161a21", highlightthickness=0, **kwargs)
        self.titulo = titulo
        self.unidade = unidade
        self.minimo = minimo
        self.maximo = maximo
        self.cor = cor
        self.valor = minimo
        self.bind("<Configure>", lambda _event: self.desenhar())

    def atualizar(self, valor):
        self.valor = max(self.minimo, min(self.maximo, valor))
        self.desenhar()

    def desenhar(self):
        self.delete("all")
        largura = max(self.winfo_width(), 180)
        altura = max(self.winfo_height(), 150)
        cx, cy = largura / 2, altura * 0.63
        raio = min(largura * 0.40, altura * 0.52)
        caixa = (cx - raio, cy - raio, cx + raio, cy + raio)

        # Tkinter mede ângulos no sentido anti-horário a partir da direita.
        self.create_arc(caixa, start=30, extent=120, style="arc", width=13, outline="#303640")
        proporcao = (self.valor - self.minimo) / (self.maximo - self.minimo)
        self.create_arc(
            caixa, start=30, extent=120 * proporcao, style="arc", width=13, outline=self.cor
        )
        self.create_text(cx, 22, text=self.titulo, fill="#e7edf5", font=("Arial", 11, "bold"))
        self.create_text(cx, cy - 3, text=self.formato_valor(), fill="white", font=("Arial", 21, "bold"))
        self.create_text(cx, cy + 25, text=self.unidade, fill="#9aa7b7", font=("Arial", 9))
        self.create_text(cx - raio + 3, cy + 25, text=str(int(self.minimo)), fill="#748092", font=("Arial", 8))
        self.create_text(cx + raio - 3, cy + 25, text=str(int(self.maximo)), fill="#748092", font=("Arial", 8))

    def formato_valor(self):
        return f"{self.valor:.1f}" if self.maximo <= 100 else f"{self.valor:.0f}"


class Painel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Celta — Painel de instrumentos")
        self.geometry("940x620")
        self.minsize(700, 480)
        self.configure(bg="#0e1116")
        self.dados = queue.Queue()
        self.erros = queue.Queue()
        self.parar = threading.Event()
        self.gauges = {}
        self.criar_interface()
        self.leitor = LeitorECU(PORTA, self.dados, self.erros, self.parar)
        self.leitor.start()
        self.after(100, self.atualizar_interface)
        self.protocol("WM_DELETE_WINDOW", self.fechar)

    def criar_interface(self):
        topo = tk.Frame(self, bg="#0e1116")
        topo.pack(fill="x", padx=24, pady=(18, 4))
        tk.Label(topo, text="CELTA", fg="#35d0ba", bg="#0e1116", font=("Arial", 22, "bold")).pack(side="left")
        self.status = tk.Label(topo, text="● CONECTANDO", fg="#f3bd4f", bg="#0e1116", font=("Arial", 10, "bold"))
        self.status.pack(side="right", pady=7)

        tk.Label(
            self, text=f"Telemetria da ECU  •  {PORTA}  •  {BAUDRATE} baud",
            fg="#7f8b9c", bg="#0e1116", font=("Arial", 10),
        ).pack(anchor="w", padx=27, pady=(0, 12))

        area = tk.Frame(self, bg="#0e1116")
        area.pack(fill="both", expand=True, padx=18, pady=4)
        configuracoes = [
            ("rpm", "RPM", "rpm", 0, 8000, "#45a7ff"),
            ("map", "MAP", "kPa", 0, 110, "#b786ff"),
            ("tps", "TPS", "%", 0, 100, "#35d0ba"),
            ("ect", "ECT", "°C", -20, 130, "#ff795f"),
            ("iat", "IAT", "°C", -20, 100, "#f3bd4f"),
            ("bat", "BATERIA", "V", 8, 16, "#62d66f"),
            ("consumo", "CONSUMO", "L/h", 0, 20, "#ff70ad"),
        ]
        for coluna in range(4):
            area.columnconfigure(coluna, weight=1)
        for linha in range(2):
            area.rowconfigure(linha, weight=1)

        for indice, config in enumerate(configuracoes):
            chave, titulo, unidade, minimo, maximo, cor = config
            gauge = Gauge(area, titulo, unidade, minimo, maximo, cor, width=210, height=190)
            gauge.grid(row=indice // 4, column=indice % 4, sticky="nsew", padx=5, pady=5)
            self.gauges[chave] = gauge

    def atualizar_interface(self):
        try:
            while True:
                valores = self.dados.get_nowait()
                for chave, valor in valores.items():
                    self.gauges[chave].atualizar(valor)
                self.status.config(text="● CONECTADO", fg="#62d66f")
        except queue.Empty:
            pass

        try:
            erro = self.erros.get_nowait()
            self.status.config(text="● ERRO DE CONEXÃO", fg="#ff665f")
            messagebox.showerror("Falha na comunicação", f"Não foi possível ler a ECU:\n\n{erro}")
        except queue.Empty:
            pass
        self.after(100, self.atualizar_interface)

    def fechar(self):
        self.parar.set()
        self.destroy()


if __name__ == "__main__":
    Painel().mainloop()
