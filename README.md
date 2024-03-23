How to build DEB
---
`apt install debhelper`

`./build.sh`


How to install DEB
---
`apt install -y --reinstall ./virgo-ups_1.0.0_all.deb`


How to start service
---
`systemctl enable --now virgo-ups`
