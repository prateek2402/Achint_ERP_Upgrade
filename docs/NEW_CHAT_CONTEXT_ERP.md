# Achint ERP Upgrade — project context

**Folder:** `E:\Achint_ERP_Upgrade\` | **Port:** 3000 | **Health:** `/api/healthz`

## Scope

Achint ERP is the main accounts / dispatch / PO system for Achint Chemicals. Keep ERP work in this repo and keep Achint Lab work in `E:\Achint_Lab\`.

## Relationship to Achint Lab

- Achint Lab is a separate app on port 3001.
- Lab reads ERP data only via `GET /api/purchase-orders` and `GET /api/clients`.
- ERP should remain the source for clients and purchase orders.
- Do not mix Lab UI/backend changes into this repo unless explicitly asked.

## Current server commands

```powershell
cd E:\Achint_ERP_Upgrade
.\scripts\server.ps1 start
.\scripts\server.ps1 stop
.\scripts\server.ps1 status
```

ERP normally starts network-bound on `0.0.0.0:3000`; local URL is `http://localhost:3000/`.

## Useful endpoints

- `GET /` — ERP UI
- `GET /api/healthz` — health probe
- `GET /api/clients` — client list
- `GET /api/purchase-orders` — PO list used by Achint Lab

## Notes for a fresh ERP chat

- The repo currently has many unrelated modified/untracked files. Do not revert them unless explicitly instructed.
- Use ERP UI style as the visual reference for Achint Lab, but keep app code separate.
- If both apps are needed:

```powershell
cd E:\Achint_ERP_Upgrade
.\scripts\server.ps1 start

cd E:\Achint_Lab
.\scripts\server.ps1 start
```

Then verify:

- ERP: `http://127.0.0.1:3000/api/healthz`
- Lab: `http://127.0.0.1:3001/api/health`
