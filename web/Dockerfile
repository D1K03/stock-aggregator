# Build context is web/, not the repository root: this image shares nothing with
# the Python one and copying the whole tree would only slow the build.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app
# Off unless a local build turns it on. Next inlines NEXT_PUBLIC_* at build
# time, so with this unset the sign-in shortcut is not merely hidden in the
# production image, it is not compiled into the bundle at all.
ARG NEXT_PUBLIC_LOCAL_LOGIN=""
ENV NEXT_PUBLIC_LOCAL_LOGIN=$NEXT_PUBLIC_LOCAL_LOGIN
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root, and the same uid the Python image uses so the two are consistent.
RUN addgroup -g 10001 nodejs && adduser -u 10001 -G nodejs -S nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
