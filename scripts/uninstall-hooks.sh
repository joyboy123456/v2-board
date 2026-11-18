#!/bin/sh
exec nice -n 19 python3 "$(nice -n 19 dirname "$0")/manage-hooks.py" uninstall "$@"
