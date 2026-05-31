# Disaster Recovery - k3s Homelab

1. **Instalar OS y k3s:** Instalar Rocky Linux y levantar k3s limpio.
2. **Clonar repositorio:** `git clone https://github.com/ouarit/homelab-k8s.git`
3. **Restaurar Llave:** Aplicar la llave de Sealed Secrets desde el backup seguro ANTES de instalar el controller.
   `kubectl apply -f ruta/sealed-secrets-master-key.yaml`
4. **Instalar Sistema Base:** `kubectl apply -f system/`
5. **Instalar Aplicaciones:**
   `kubectl apply -f apps/`
