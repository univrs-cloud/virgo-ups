#!/bin/bash

set -e

mkdir -p dist
dpkg-buildpackage --build=binary --no-sign
mv ../virgo-ups*.deb ../virgo-ups*.buildinfo ../virgo-ups*.changes dist
