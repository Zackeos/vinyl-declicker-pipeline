import serial
import time

SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 9600

print(f"Connecting to Arduino on {SERIAL_PORT}...")
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print("Connected successfully!")
    print("Listening for switch states... (Press Ctrl+C to stop)")
    print("-" * 50)
    
    while True:
        line = ser.readline().decode('utf-8').strip()
        if line:
            print(f"[RAW SERIAL] {line}", flush=True)
            
except serial.SerialException as e:
    print(f"Failed to connect: {e}")
except KeyboardInterrupt:
    print("\nExiting.")
    if 'ser' in locals() and ser.is_open:
        ser.close()
