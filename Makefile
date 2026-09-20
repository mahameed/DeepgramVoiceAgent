COMPOSE  := docker compose
WRANGLER := npx wrangler
NAME     := voice-agent
CONTAINER_NAME := $(NAME)-voiceagentcontainer
PORT     := 8080

.PHONY: help build up down logs health restart clean deploy secret undeploy destroy

help:
	@echo "Local Docker"
	@echo "  make build     Build the linux/amd64 image"
	@echo "  make up        Build and start the container in the background"
	@echo "  make down      Stop and remove the local container"
	@echo "  make logs      Follow local container logs"
	@echo "  make health    GET /health on localhost:$(PORT)"
	@echo "  make restart   Restart the local container"
	@echo "  make clean     Stop local stack and remove image/volumes"
	@echo
	@echo "Cloudflare"
	@echo "  make secret    Set DEEPGRAM_API_KEY as a Worker secret"
	@echo "  make deploy    Deploy Worker + container image"
	@echo "  make undeploy  Delete the container application, then the Worker"

build:
	$(COMPOSE) build

up: build
	$(COMPOSE) up -d
	@echo "Listening on http://localhost:$(PORT)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

health:
	curl -sf http://localhost:$(PORT)/health && echo

restart:
	$(COMPOSE) restart

clean:
	$(COMPOSE) down --rmi local --volumes --remove-orphans
	-docker image rm $(NAME) 2>/dev/null

secret:
	$(WRANGLER) secret put DEEPGRAM_API_KEY

deploy:
	npm install
	$(WRANGLER) deploy

undeploy destroy:
	@set -eu; \
	applications="$$( $(WRANGLER) containers list --json )"; \
	container_ids="$$(printf '%s' "$$applications" | CONTAINER_NAME="$(CONTAINER_NAME)" node -e 'const fs = require("fs"); const applications = JSON.parse(fs.readFileSync(0, "utf8")); for (const application of applications) if (application.name === process.env.CONTAINER_NAME) console.log(application.id)')"; \
	if [ -z "$$container_ids" ]; then \
		echo "No Cloudflare container application named $(CONTAINER_NAME) found."; \
	else \
		for container_id in $$container_ids; do \
			echo "Deleting Cloudflare container application $$container_id..."; \
			CI=1 $(WRANGLER) containers delete "$$container_id"; \
		done; \
	fi; \
	echo "Deleting Cloudflare Worker $(NAME)..."; \
	$(WRANGLER) delete --name "$(NAME)" --force
