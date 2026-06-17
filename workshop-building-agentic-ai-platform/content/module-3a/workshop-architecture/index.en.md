---
title: "Registry Architecture"
weight: 41
---

This page explains what's running in your environment and how the components connect. Refer back here if you need to understand a component during the hands-on steps.

## Architecture Diagram

![MCP Gateway & Registry architecture — CloudFront, ECS Fargate services, Cognito, DocumentDB](/static/img/module-3/registry-architecture.png)

## How Requests Flow

1. **Browser → CloudFront → ALB → NGINX → Auth Server → Registry** — all UI and API requests follow this path. CloudFront provides HTTPS, NGINX routes by path, and the Auth Server validates your session before forwarding to the Registry.

2. **Auth Server → Cognito** — when you log in, the Auth Server redirects to the Cognito Hosted UI. After authentication, Cognito returns a JWT with your group memberships. The Auth Server exchanges the authorization code and establishes a session.

3. **Registry → DocumentDB** — server registrations, agent cards, skills, and access control scopes are stored in Amazon DocumentDB (MongoDB-compatible).

4. **Registry → MCP servers** — the MCP Gateway (`mcpgw`) reaches backend MCP servers via ECS Service Connect (internal DNS, no public exposure).

## Components

| Component | What It Does |
|---|---|
| **Registry UI** | Gradio dashboard — browse, register, and manage MCP servers and A2A agents |
| **Registry API** | FastAPI backend — registration, discovery, semantic search, access control |
| **Auth Server** | OAuth2/OIDC proxy — bridges Cognito with the Registry for both browser sessions and M2M tokens |
| **MCP Gateway** | Aggregates tools from registered MCP servers behind a single endpoint |
| **NGINX** | Reverse proxy sidecar — routes CloudFront traffic to the Auth Server |

## Identity Model

| Who | How They Authenticate | Token Source |
|---|---|---|
| **Human users** (you) | Browser login → Cognito Hosted UI → session cookie | Auth Server exchanges Cognito auth code for JWT |
| **M2M agents** (Module 4) | Client credentials → Auth Server token endpoint | Cognito M2M client ID + secret → access token |
| **API scripts** (this module) | Static API token in `Authorization: Bearer` header | Pre-generated token stored in Secrets Manager |

All three paths result in a token that the Auth Server validates. Cognito groups determine what each identity can see and do.

::alert[Module 3a (Tools Gateway) reuses the same Cognito User Pool for AgentCore Gateway authentication (CUSTOM_JWT authorizer). Groups created here carry over.]{type="info"}

## AWS Infrastructure

All services run on Amazon ECS Fargate in private subnets across 3 Availability Zones. Only CloudFront and the ALB are publicly accessible.

| AWS Service | Purpose |
|---|---|
| **Amazon ECS Fargate** | Runs all containerized services (Registry, Auth Server, MCP Gateway, demo servers) |
| **Amazon CloudFront** | HTTPS entry point |
| **Application Load Balancer** | Routes traffic to ECS services |
| **Amazon Cognito** | User authentication, group-based access control |
| **Amazon DocumentDB** | Stores registrations, agent cards, access control scopes |
| **AWS Secrets Manager** | Admin password, API tokens, M2M credentials |
| **Amazon Managed Prometheus + Grafana** | Observability dashboards |
