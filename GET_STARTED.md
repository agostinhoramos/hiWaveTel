sudo apt update
sudo apt install software-properties-common -y
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install python3.11 python3.11-venv python3.11-dev -y


mkdir hiWaveTel

cd hiWaveTel

python3.10 -m venv .venv

source .venv/bin/activate

pip install --upgrade pip

python3.10 -m pip install -r requirements.txt
# pip freeze > requirements.txt

django-admin startproject config .

mkdir apps

cd apps

django-admin startapp core 
# python3.10 manage.py startapp core

INSTALLED_APPS = [
    ...
    'apps.core',
]
apps/core/apps.py
name = 'apps.core'

python3.10 manage.py makemigrations
python3.10 manage.py migrate

python3.10 manage.py runserver

deactivate

rm -rf .venv