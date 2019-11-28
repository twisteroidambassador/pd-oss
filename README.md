# PD-OSS: On-Screen Slider for Project DIVA Arcade Future Tone

## Features

* Supports mouse and multitouch input
* Lights up with colors

## How to Use

PD-OSS is developed and tested on Windows. It should run on other operating
systems with minimal changes; see notes below.

### Prepare the serial port

This app communicates via COM12. Connect COM12 on the machine running PD-OSS
to COM11 on the machine running PDAFT. If it happens to be the same machine, 
use a virtual null modem cable such as 
[com0com](https://sourceforge.net/projects/com0com/).
(If you're on a fresh install of a recent version of Windows 10, you may need
to use the signed driver installer of com0com from 
[here](https://code.google.com/archive/p/powersdr-iq/downloads), instead of
the official site. 
[Reason](https://sourceforge.net/p/com0com/bugs/32/)
[Link source](https://reactos.org/wiki/Com0com))

### Install dependencies and run app

#### The easy way

Coming Soon^TM

#### The hard way

* Create a new Python 3 virtualenv. PD-OSS is developed on Python 3.7. Other
versions of Python 3 may also work.
* Install dependencies: 
[Kivy](https://kivy.org/doc/stable/installation/installation-windows.html),
[pySerial](https://pypi.org/project/pyserial/)
and
[pywin32](https://pypi.org/project/pyserial/).
* Run `serial.py` from this repository under the virtualenv.

### Run the game
The game should detect the slider, and print out "TOUCH SLIDER BD: OK" on the
loading screen. Once the game is loaded, you should see rainbow colors on the
slider.

## Cross-Platform Notes
Both Kivy and pySerial are supported on many operating systems. pywin32, which
is obviously Windows specific, is used to make the slider window ignore focus
when clicked, so that when you touch the slider, the slider window does not
steal focus away from the game window.

If references to pywin32 is removed, PD-OSS should run on Linux and other
platforms, but the game window constantly losing focus may interfere with 
gameplay.

## Reference

Serial protocol used by the slider: 
https://gist.github.com/dogtopus/b61992cfc383434deac5fab11a458597

## License

Files in this repository are licensed under GNU GPLv3.
