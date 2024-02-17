#!/bin/bash

mkdir -p deb-packages
dpkg-buildpackage --build=binary --no-sign
mv ../virgo-ups*.deb ../virgo-ups*.buildinfo ../virgo-ups*.changes deb-packages
