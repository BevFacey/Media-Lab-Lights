# Raspberry Pi Pico W with MicroPython
import network
import time
import machine
import _thread
import gc
from umqtt.simple import MQTTClient

# ---------------- CONFIGURATION ----------------
AIO_SERVER    = "io.adafruit.com"
AIO_PORT      = 1883
AIO_USER      = ""
AIO_KEY       = ""
AIO_FEED      = f"{AIO_USER}/feeds/media-lab-dmx"
WIFI_SSID     = ""
WIFI_PASS     = ""

DMX_TX_PIN         = 4
NUM_LIGHTS         = 14
CHANNELS_PER_LIGHT = 26

# Allocate 513-byte buffer (Index 0 = 0x00 DMX Start Code, 1-512 = DMX Channels)
dmx_data = bytearray(513)
dmx_data[0] = 0x00

# Initialize Hardware UART1 globally
uart = machine.UART(1, baudrate=250000, bits=8, parity=None, stop=2, tx=machine.Pin(DMX_TX_PIN))

# ---------------- DMX UTILITIES ----------------
def initialize_fixture_defaults():
    """Sets Strobe=0 and Master Dimmer=255 for all 14 lights by default."""
    for l in range(NUM_LIGHTS):
        base = 1 + (l * CHANNELS_PER_LIGHT)
        dmx_data[base + 24] = 0    # Channel 25: Strobe Off
        dmx_data[base + 25] = 255  # Channel 26: Master Dimmer Full

def set_segment_rgb(light, segment, r, g, b):
    """Sets RGB for a specific light (1-14) and segment (1-8) with 0-255 value clamping."""
    global dmx_data
    base = 1 + ((light - 1) * CHANNELS_PER_LIGHT)
    seg_offset = (segment - 1) * 3
    dmx_data[base + seg_offset]     = max(0, min(255, int(r)))  # Red
    dmx_data[base + seg_offset + 1] = max(0, min(255, int(g)))  # Green
    dmx_data[base + seg_offset + 2] = max(0, min(255, int(b)))  # Blue

def send_dmx():
    """Outputs a stable DMX frame with a timed 9600 baud BREAK pulse."""
    # 1. Switch to 9600 baud to output a 1.04ms BREAK pulse
    uart.init(baudrate=9600, bits=8, parity=None, stop=2)
    uart.write(b'\x00')
    
    # 2. Wait 1.2ms for the 9600 baud byte to finish outputting before changing clocks
    time.sleep_us(1200)
    
    # 3. Re-init to 250,000 baud and transmit full payload
    uart.init(baudrate=250000, bits=8, parity=None, stop=2)
    uart.write(dmx_data)
    uart.flush()

# Set default master channels
initialize_fixture_defaults()

# ---------------- CORE 1 THREAD (HARDWARE DMX ENGINE) ----------------
def dmx_output_thread():
    """Runs independently on Pico Core 1 to guarantee uninterrupted 40 Hz DMX output."""
    while True:
        send_dmx()
        time.sleep_ms(25)  # 40 Hz refresh rate

# ---------------- MQTT CALLBACK ----------------
def mqtt_callback(topic, msg):
    try:
        raw_cmd = msg.decode('utf-8').strip()
        print(f"\n[MQTT Received] -> '{raw_cmd}'")

        parts = [int(x) for x in raw_cmd.split(',')]
        if len(parts) == 5:
            light, segment, r, g, b = parts
            
            # Handle Light targeting (0 = ALL lights, 1-14 = specific light)
            target_lights = range(1, NUM_LIGHTS + 1) if light == 0 else [light]

            if light == 0 or (1 <= light <= NUM_LIGHTS):
                for l in target_lights:
                    if 1 <= segment <= 8:
                        set_segment_rgb(l, segment, r, g, b)
                    elif segment == 0:
                        for seg in range(1, 9):
                            set_segment_rgb(l, seg, r, g, b)
                    else:
                        print(f"   REJECTED: Segment {segment} out of bounds (use 0 for all, or 1-8)")
                        return
                
                print(f"   SUCCESS: Light {light} (Segment {segment}) set to R:{r} G:{g} B:{b}")
            else:
                print(f"   REJECTED: Light {light} out of bounds (use 0 for all, or 1-{NUM_LIGHTS})")
        else:
            print("   REJECTED: Format must be 'light,segment,r,g,b'")
    except Exception as e:
        print("   ERROR Parsing Command:", e)

# ---------------- NETWORK CONNECT ----------------
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(WIFI_SSID, WIFI_PASS)

print("Connecting to Wi-Fi...", end="")
while not wlan.isconnected():
    time.sleep(0.5)
    print(".", end="")
print("\nWi-Fi Connected! IP:", wlan.ifconfig()[0])

# Launch DMX engine on Core 1
_thread.start_new_thread(dmx_output_thread, ())
print("Hardware DMX Engine running independently on Core 1!")

# ---------------- MQTT SETUP (CORE 0) ----------------
client = MQTTClient(
    client_id="pico_dmx_node",
    server=AIO_SERVER,
    port=AIO_PORT,
    user=AIO_USER,
    password=AIO_KEY,
    keepalive=60
)

client.set_callback(mqtt_callback)
client.connect()
client.subscribe(AIO_FEED)
client.sock.setblocking(False)

print(f"Subscribed to Adafruit IO Feed: {AIO_FEED}")
print("Ready for web/URL commands!")

# ---------------- MAIN LOOP (CORE 0: NETWORK & MQTT ONLY) ----------------
last_gc_time = time.ticks_ms()

while True:
    try:
        client.check_msg()
    except OSError:
        # Reconnect if socket drops
        try:
            client.connect()
            client.subscribe(AIO_FEED)
        except Exception:
            time.sleep(1)
    except Exception:
        pass

    # Clean up memory periodically without affecting Core 1 DMX timing
    if time.ticks_diff(time.ticks_ms(), last_gc_time) > 10000:
        gc.collect()
        last_gc_time = time.ticks_ms()

    time.sleep_ms(10)
