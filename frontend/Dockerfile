# Build the SPA, then serve the static bundle from nginx.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

# Vite inlines VITE_* at build time, so the bot username has to be present here
# rather than at run time. Without it the public site can tell a visitor to use
# Telegram but cannot link them to it — which is a dead end, since Telegram is
# the only way to sign in.
ARG VITE_TELEGRAM_BOT_USERNAME=""
ENV VITE_TELEGRAM_BOT_USERNAME=$VITE_TELEGRAM_BOT_USERNAME
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
