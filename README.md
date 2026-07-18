# Disaster Recovery - k3s Homelab

1. **Instalar OS y k3s:** Instalar Rocky Linux y levantar k3s limpio.
2. **Clonar repositorio:** `git clone https://github.com/ouarit/homelab-k8s.git`
3. **Restaurar Llave:** Aplicar la llave de Sealed Secrets desde el backup seguro ANTES de instalar el controller.
   `kubectl apply -f ruta/sealed-secrets-master-key.yaml`
4. **Instalar Sistema Base:** `kubectl apply -f system/`
5. **Instalar Aplicaciones:**
   `kubectl apply -f apps/`

## Filosofía de diseño: secretos

**Todo secreto que toca este repositorio se sella con Sealed Secrets antes de
commitear — nunca se excluye vía `.gitignore` como "solución" temporal.**

Por qué: un secreto en `.gitignore` funciona mientras despliegas manualmente
desde tu laptop, pero rompe el modelo GitOps — si el clúster se reconstruye
desde este repo (ver Disaster Recovery arriba), ese recurso simplemente no
existe. El repo debe ser la fuente de verdad completa, incluyendo secretos,
sin depender de estado que solo vive en tu máquina.

Patrón a seguir en cualquier app nueva (ver `apps/jobsearch/manifests/06-ia-sealedsecret.yaml`
como referencia):

```bash
kubectl create secret generic <nombre> -n <namespace> --dry-run=client \
  --from-literal=clave='valor' -o yaml > secret-plain.yaml
kubeseal --format yaml < secret-plain.yaml > manifests/NN-<nombre>-sealedsecret.yaml
rm secret-plain.yaml
```

Lo único que nunca se commitea, ni siquiera sellado, es la llave maestra de
Sealed Secrets (`sealed-secrets-master-key.yaml`) — esa vive solo en tu
backup seguro, tal como indica el paso 3 del Disaster Recovery.
