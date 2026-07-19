# jobsearch v2 — búsqueda automática de vacantes, sin mantenimiento manual de keywords

JobSpy + descubrimiento web de bajo perfil + scoring por IA, todo derivado
automáticamente de tu CV. Ver `ARCHITECTURE.md` para el detalle técnico y qué
cambió respecto a la versión anterior.

## ⚠️ Antes de desplegar: rota tus credenciales

Si vienes del proyecto anterior, tu `CF_CLIENT_ID`, `CF_CLIENT_SECRET` e
`IA_BEARER_TOKEN` quedaron expuestos en texto plano en tu `.env`. Genera
valores nuevos antes de continuar — sellar un secreto que ya se filtró no lo
vuelve seguro otra vez.

## 1. Tu CV

Pega tu CV en LaTeX (o texto plano) en `cv.txt`, tal cual. `src/cv_extract.py`
se encarga de limpiarlo antes de mandarlo a la IA — no necesitas quitarle el
formato tú. Haz commit de este archivo (igual que antes, evita datos muy
sensibles como tu dirección exacta si el repo es público).

**No necesitas definir keywords ni títulos de puesto en ningún lado.** La
primera vez que corra cualquiera de los dos CronJobs, va a leer tu CV y
generar automáticamente `profile.json` en el PVC con los términos de
búsqueda, exclusiones de seniority, y queries de discovery. Si vuelves a
editar `cv.txt`, el siguiente `apply` + corrida regenera el perfil solo
(detecta el cambio por hash).

## 2. Configuración pública

`manifests/02-configmap-env.yaml` — ubicación, umbral de match
(`MIN_MATCH_SCORE`), agresividad del discovery, y config de IA. Todo lo que
ahí queda es dato objetivo (ciudades, hints de remoto, límites de tasa), no
juicio sobre tu perfil.

## 3. Secretos (Sealed Secrets)

Mismo patrón que ya usas en `homelab-k8s`:

```bash
kubectl create secret generic jobsearch-secrets -n jobsearch --dry-run=client \
  --from-literal=GOTIFY_TOKEN='tu_token_nuevo' \
  --from-literal=CF_CLIENT_ID='tu_client_id_nuevo' \
  --from-literal=CF_CLIENT_SECRET='tu_client_secret_nuevo' \
  --from-literal=IA_BEARER_TOKEN='tu_token_ia_nuevo' \
  -o yaml > secret-plain.yaml

kubeseal --format yaml < secret-plain.yaml > manifests/06-secret-env-sealed.yaml
rm secret-plain.yaml
```

Este paso queda fuera del `.gitignore` a propósito: el `SealedSecret` final
sí se commitea (ya está cifrado), solo el intermedio `secret-plain.yaml` se
descarta.

## 4. Despliegue

```bash
kubectl apply -k .
```

Kustomize arma los ConfigMaps (`jobsearch-src`, `jobsearch-cv`) directamente
desde `src/*.py` y `cv.txt`, y aplica todo lo demás. No hay script
intermedio: si editas código, el siguiente `apply` ya lo refleja.

## 5. Dashboard

`jobs.ouarit.lol` (o tu dominio) apuntando al Ingress. Muestra el
`match_score` por vacante, la razón corta que dio la IA, y qué términos está
usando actualmente el perfil generado.

## 6. Pruebas manuales

```bash
# Forzar un ciclo de discovery sin esperar el cron
kubectl create job --from=cronjob/jobsearch-discovery -n jobsearch test-discovery-01

# Forzar el scraper (JobSpy + consolidación + dashboard)
kubectl create job --from=cronjob/jobsearch-scraper -n jobsearch test-jobspy-01

# Ver el perfil que se generó desde tu CV
kubectl exec -it -n jobsearch <pod-nginx> -- cat /usr/share/nginx/html/../../data/profile.json
```

## 7. Desarrollo local (opcional)

```bash
uv sync
uv run python src/cv_extract.py cv.txt   # probar la extracción de tu CV
```
