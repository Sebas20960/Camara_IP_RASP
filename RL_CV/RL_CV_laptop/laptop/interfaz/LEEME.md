# Monitor de tráfico — programa de escritorio (laptop) · V_final_laptop

Ventana que muestra lo que manda la Raspberry Pi: el video de las 4 cámaras con las cajas
de YOLO, los conteos en vivo, el historial con gráficas y la configuración de la conexión.

## Abrirlo

- **Doble clic en `iniciar_interfaz.bat`** (la primera vez instala lo que falte).
- O por consola: `python app.py [--puerto 8000] [--token cambia-esto]`
- Sin Raspberry, con datos de prueba: `python app.py --falso`

## Conectar la Raspberry (misma red)

1. La laptop y la Pi deben estar en la **misma red** (mismo Wi-Fi o cable).
2. Abre el programa. La **primera vez Windows pregunta por el firewall**: marca
   **«Redes privadas»** y pulsa **Permitir**. Si lo negaste: *Firewall de Windows Defender →
   Permitir una aplicación* → habilita Python en redes privadas.
3. En la pestaña **Conexión** está el comando listo, con la IP de la laptop, el puerto y el
   token. Pulsa **Copiar** y ejecútalo en la Pi (carpeta `~/V_final_laptop/raspberry`):

   ```
   python conteo4_laptop.py --laptop IP_LAPTOP:8000 --token-laptop cambia-esto
   ```

   (o pon esos valores en `LAPTOP_URL` / `LAPTOP_TOKEN` dentro de `conteo4_laptop.py`).
4. En la cabecera aparece **«Raspberry en línea»** y, en *En vivo*, el video.

## Cámaras simuladas (sin cámaras reales)

En **Conexión → Cámaras simuladas**: elige hasta 4 videos (de `laptop/videos`; si está
vacía, el programa propone los de `V_final_final/laptop/videos`), ajusta ancho / cuadros/s /
calidad y pulsa **Iniciar cámaras**. Se lanza `camfalsa4.py` y aparecen las direcciones
(`IP_LAPTOP:8080`, `:8081`…) para poner en `CAMARAS` de la Pi; también se agregan al comando
de arriba. *Detener* (o cerrar el programa) apaga el simulador.

## Pestañas

- **En vivo**: video 2×2 (doble clic en una cámara = verla en grande), estado de la Pi
  (temperatura en rojo desde 80 °C, inferencias/s, kbps y cuadros/s recibidos), vehículos en
  imagen, pasaron en la última hora y hoy, barras por categoría y una tarjeta por cámara.
  *Pausar video* le dice a la Pi que deje de mandarlo (los conteos siguen).
- **Historial**: líneas por cámara (1 h / 6 h / 24 h / 7 días), «Vehículos que pasaron» o
  «En imagen (promedio)»; los huecos son periodos sin datos. Pasa el cursor para ver valores.
  Barras por categoría, *Ver tabla* y *Exportar CSV…*. El historial se guarda en
  `historial_laptop.db` (SQLite, junto a `nucleo.py`).
- **Conexión**: comando para la Pi, puerto, token y retraso de visualización (más retraso =
  cajas más suaves), y el simulador de cámaras.

## Problemas comunes

- **La Pi no conecta**: revisa firewall, que la IP sea la de la red correcta (`ipconfig`),
  y que puerto y token coincidan en ambos lados.
- **«No se pudo abrir el puerto»**: otro programa lo usa; cambia el puerto en *Conexión*.

Archivos: `app.py` (ventana), `nucleo.py` (red, buffers, historial, simulador),
`_nucleo_falso.py` (datos de prueba para `--falso`), `pi_falsa.py` (imita a la Raspberry sin YOLO:
`python pi_falsa.py --destino localhost:8000 VIDEO.mp4`).

Los ajustes del programa (videos elegidos, puerto del simulador…) se guardan en el registro
de Windows como *MonitorTrafico / InterfazLaptopV*, separados de los de V_final_final.
