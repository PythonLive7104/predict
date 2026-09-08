FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# scipy/numpy ship manylinux wheels, so no build toolchain is needed here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No `|| true`. The manifest storage means a silent failure here does not
# surface until a user loads /admin/ in production and gets a 500 from a
# missing manifest entry — a build that cannot collect static is a broken
# build and should say so. SECRET_KEY is supplied because settings require
# one to import, and this value never leaves the build step.
RUN DJANGO_SECRET_KEY=build-time-only-not-a-secret \
    DJANGO_DEBUG=False \
    python manage.py collectstatic --noinput

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
