# app-service-028 — explainer

## How to investigate

Start where App Service told you the failure is: `fixtures/docker-log.txt`.
The host log shows the container image pulling fine (`Image pulled, size
148.2 MiB`) and then failing its startup ping on a specific port —
`didn't respond to HTTP pings on port: 80, failing site start`, repeated
across three restart attempts until the site is marked unhealthy. The same
line shows the exact `docker run` invocation App Service used, including
`-e WEBSITES_PORT=80`. So the platform is pinging port 80 by configuration,
not by accident.

Next ask what the container is actually listening on. `fixtures/app-log.txt`
is the in-container gunicorn log: `Listening at: http://0.0.0.0:5000`, with
`GET /healthz 200 OK` responses every few seconds — until a `SIGTERM`
arrives roughly 40–80 seconds in. That SIGTERM is App Service killing the
container because the ping on port 80 never got an answer. The app is
healthy; the platform is just knocking on the wrong door.

Now confirm where the "80" comes from: `fixtures/az-webapp-config-appsettings.json`
shows the app setting `WEBSITES_PORT` is still `"80"`. That setting was
correct for the previous image but the new one changed its listen port.
`fixtures/Dockerfile` proves the new image's intent: `EXPOSE 5000` and the
CMD `gunicorn -b 0.0.0.0:5000`. The deploy changed the listen port from 80
to 5000 without updating `WEBSITES_PORT` to match.

Finally, rule out the platform itself: `fixtures/az-webapp-show.json` shows
`state: Running`, `availabilityState: Normal`, `provisioningState: Succeeded`
and a healthy P2v3 plan reference. The host is fine — this is purely a
port contract mismatch between the image and the app setting.

## Why the red herring misleads

A 503 right after a deploy smells like capacity or a broken plan, and
`fixtures/az-webapp-show.json` feeds that instinct: it surfaces
`numberOfWorkers: 2` and the `asp-quotes-prod` plan, inviting a
"scale out the plan" conclusion. The healthy CPU/memory in the symptom
report even agrees the plan is not exhausted — and that is exactly the
point: the host never fails. `fixtures/app-log.txt` shows the application
serving `/healthz 200` happily; `fixtures/docker-log.txt` shows the image
pull succeeding. Nothing about scaling or the plan is involved — the
container is killed on a port ping it never hears because the platform is
pinging 80 while the app answers on 5000.

## Root cause

The new image `quotes-api:2026.10.30` listens on 5000
(`fixtures/Dockerfile`: `EXPOSE 5000`, `gunicorn -b 0.0.0.0:5000`), but the
`WEBSITES_PORT` app setting on `app-quotes` is still `"80"`
(`fixtures/az-webapp-config-appsettings.json`), so App Service pings port 80
and fails the site start (`fixtures/docker-log.txt`: "didn't respond to
HTTP pings on port: 80"). The deploy changed the container's listen port
without updating the setting that tells App Service which port to ping.

## Fix

Update `WEBSITES_PORT` on `app-quotes` from 80 to 5000 and restart the app:

```bash
az webapp config appsettings set \
  --resource-group rg-quotes-prod \
  --name app-quotes \
  --settings WEBSITES_PORT=5000

az webapp restart --resource-group rg-quotes-prod --name app-quotes
```

Safe and reversible: it is a single app setting, takes effect on restart,
and matches the image's declared listen port. Going forward, any image
that changes its listen port must change `WEBSITES_PORT` in the same
deploy.

## Prevention

Make the port contract explicit and checked, not tribal knowledge. Two
concrete guardrails:

1. **Pin the port in the image and derive the setting from it.** Have the
   Dockerfile emit `ENV PORT=5000` and keep a CI step (or `docker run` +
   health check in the deploy pipeline) that starts the freshly built
   image locally and asserts `/healthz` answers on the port declared by
   `EXPOSE` — the exact check that would have caught this deploy.
2. **Alert on the signature, not just the symptom.** The availability
   alert fired 5 minutes after the first failure, but the host log already
   said `didn't respond to HTTP pings on port: 80`. A log alert on
   App Service container logs matching "didn't respond to HTTP pings"
   pages the right failure mode immediately, instead of letting the
   restart loop mask it as generic 503s.

## Further reading

- [Configure a custom container for Azure App Service (WEBSITES_PORT)](https://learn.microsoft.com/azure/app-service/configure-custom-container)
- [Understand App Service health checks and site start behavior](https://learn.microsoft.com/azure/app-service/monitor-infra-health-status)
- [Dockerfile EXPOSE reference](https://docs.docker.com/reference/dockerfile/#expose)
