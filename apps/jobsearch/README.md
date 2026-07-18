# jobsearch — búsqueda automática de vacantes en tu homelab

Scraper automatizado + Descubrimiento Dinámico Web + LLM Scoring (Ollama) + sitio web estático servido en tu clúster.

Todo corre en el namespace `jobsearch` de tu clúster k3s, usando recursos modestos y aislando credenciales mediante GitOps y Sealed Secrets.

## Arquitectura

El proyecto consta de dos cargas de trabajo independientes para maximizar la recolección sin arriesgar bloqueos ni saturar la IA:

1. **JobSpy (`script.py`)**: Busca rápidamente en plataformas tradicionales (LinkedIn, Indeed, Google Jobs) basándose en las últimas horas transcurridas. Consolida los resultados, hace el merge seguro del CSV temporal del discovery y regenera el sitio web (HTML estático).
2. **Web Discovery (`discovery.py`)**: Realiza búsquedas orgánicas en internet (ej. DuckDuckGo HTML, Bing) para encontrar publicaciones directas o portales de empleos corporativos que omiten las bolsas de trabajo. Emplea un riguroso sistema de historial, respeto al `robots.txt` y backoff ante CAPTCHAs. Descarga las páginas y llama a la IA para que extraiga la vacante y genere un *match score* en una sola pasada.

## 1. Configuración Inicial (Privacidad Primero)

Toda la lógica sensible vive de forma local en tu máquina y **no debe** ser subida a Git.

1. Crea tu archivo de entorno a partir de la plantilla:
   ```bash
   cp .env.template .env
   ```
2. Rellena el `.env` con tus palabras clave, exclusiones y los secretos (Gotify Token, credenciales de Cloudflare para tu Ollama remoto, etc.).
3. Crea un archivo llamado `cv.txt` y pega ahí el texto plano de tu currículum. La IA lo usará para puntuar cada vacante.

*(Nota: Tanto `.env` como `cv.txt` están en el `.gitignore` por seguridad).*

## 2. Despliegue Automatizado

Toda la magia del despliegue (conversión a manifiestos e inyección de secretos) la hace `deploy.py`.

Para mantener buenas prácticas de GitOps usando **Sealed Secrets**, puedes realizar el cifrado del secreto de forma completamente local y offline. Solo debes pasar la ruta a la llave pública de tu controlador:

```bash
python deploy.py --cert sealed-secrets-pub-cert.pem
```

*Si omites `--cert` o el archivo no existe, el script intentará conectarse activamente al clúster para sellar el secreto.*

¿Qué hace este script?
1. Genera los ConfigMaps de configuración pública y del código fuente (Python y CV).
2. Cifra tus variables secretas en un SealedSecret seguro para Git.
3. Actualiza dinámicamente los horarios de los CronJobs.
4. Despliega la infraestructura aplicando un `kubectl apply -k manifests/`.

## 3. Consultar tu Dashboard Web

Asegúrate de tener un registro DNS local o una entrada en tu `/etc/hosts` apuntando `jobs.ouarit.lol` (o la URL que definiste) hacia el Ingress controller de tu clúster.
Una vez resuelto, tu dashboard mostrará el score asignado a cada vacante, chips de colores y estadísticas de tu recolección.

## 4. Pruebas Manuales (Opcional)

Si no quieres esperar al reloj de los CronJobs, puedes lanzar jobs efímeros para testear:

**Para probar el escaneo web activo (Discovery):**
```bash
kubectl create job --from=cronjob/jobsearch-discovery -n jobsearch test-discovery-01
```

**Para recolectar plataformas tradicionales, unificar bases de datos y reconstruir la UI:**
```bash
kubectl create job --from=cronjob/jobsearch-scraper -n jobsearch test-jobspy-01
```
