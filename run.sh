if [ ! -d .venv ]; then
    echo 'Please setup the virtual env and dependencies under .venv'
    exit 1
fi
source .venv/bin/activate
python digest.py
