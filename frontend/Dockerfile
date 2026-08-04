FROM python:3.12-slim
WORKDIR /app
COPY app.py admin.py store.py payments.py captcha.py vouchers.py ./
COPY static/ static/
VOLUME /var/lib/obchod
EXPOSE 8093
CMD ["python", "-u", "app.py"]
