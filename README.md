# UPS monitoring and auto-shutdown service

## Building

The build-deb script uses the dpkg-buildpackage to generate an unsigned .deb package in the `deb-packages` dir.
```
./build-deb.sh
```

## Installation

Copy the built .deb file to the appliance and install it using dpkg:
```
sudo dpkg -i virgo-ups_0.0.1_all.deb
```

Check ups monitor status:
```
sudo systemctl status ups
```
