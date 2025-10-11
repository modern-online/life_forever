from gpiozero import Button
from signal import pause

PINS = [13, 16, 26]

def trigger(i):
    print(f"button {i+1} pressed")

btns = [Button(p, pull_up=True, bounce_time=0.2) for p in PINS]
for i, b in enumerate(btns):
    b.when_pressed = (lambda i=i: trigger(i))

pause()
