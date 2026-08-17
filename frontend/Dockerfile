FROM python:3.12-slim
WORKDIR /app
# všechny moduly, ne výčet — vyjmenovaný seznam se zapomene doplnit
# a obraz pak spadne na ModuleNotFoundError až v provozu
COPY *.py ./
COPY static/ static/
VOLUME /var/lib/obchod
EXPOSE 8093
CMD ["python", "-u", "app.py"]
