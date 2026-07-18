import argparse
import base64
import os
import re
import subprocess
import sys

MANIFESTS_DIR = "manifests"

def read_env() -> dict:
    if not os.path.exists(".env"):
        print("[WARN] No se encontró el archivo .env. Por favor, cópialo desde .env.template y rellena tus datos.")
        return {}
    
    env_vars = {}
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                val = val.strip().strip('"').strip("'")
                env_vars[key.strip()] = val
    return env_vars

def read_file(filepath: str) -> str:
    if not os.path.exists(filepath):
        print(f"[WARN] No se encontró {filepath}.")
        return ""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read().strip()

def create_configmap_yaml(name: str, data: dict, filename: str) -> None:
    yaml_lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {name}",
        "  namespace: jobsearch",
        "data:"
    ]
    for key, val in data.items():
        if "\n" in val:
            yaml_lines.append(f"  {key}: |")
            for line in val.splitlines():
                yaml_lines.append(f"    {line}")
        else:
            escaped = val.replace("'", "''")
            yaml_lines.append(f"  {key}: '{escaped}'")
            
    with open(os.path.join(MANIFESTS_DIR, filename), "w", encoding="utf-8") as f:
        f.write("\n".join(yaml_lines) + "\n")

def create_secret_plain_yaml(name: str, data: dict, filename: str) -> None:
    yaml_lines = [
        "apiVersion: v1",
        "kind: Secret",
        "metadata:",
        f"  name: {name}",
        "  namespace: jobsearch",
        "type: Opaque",
        "data:"
    ]
    for key, val in data.items():
        encoded = base64.b64encode(val.encode("utf-8")).decode("utf-8")
        yaml_lines.append(f"  {key}: {encoded}")
        
    with open(os.path.join(MANIFESTS_DIR, filename), "w", encoding="utf-8") as f:
        f.write("\n".join(yaml_lines) + "\n")

def update_cronjob_schedule(filename: str, schedule: str) -> None:
    filepath = os.path.join(MANIFESTS_DIR, filename)
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r'schedule:\s*".*?"', f'schedule: "{schedule}"', content)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

def main():
    parser = argparse.ArgumentParser(description="Despliega JobSearch en Kubernetes")
    parser.add_argument("--prepare-only", action="store_true", help="Solo preparar manifiestos")
    parser.add_argument("--prepare", action="store_true", help="Solo preparar manifiestos")
    parser.add_argument("--cert", help="Ruta al certificado publico de sealed secrets para cifrado offline", default=None)
    args = parser.parse_args()

    os.makedirs(MANIFESTS_DIR, exist_ok=True)
    
    env_vars = read_env()
    cv_text = read_file("cv.txt")
    script_text = read_file("script.py")
    discovery_text = read_file("discovery.py")
    
    if not env_vars:
        print("[ERROR] Falta archivo .env")
        sys.exit(1)

    # 1. ConfigMap para los Scripts
    create_configmap_yaml("jobsearch-script", {
        "script.py": script_text,
        "discovery.py": discovery_text
    }, "02-configmap-script.yaml")

    # 2. ConfigMap para el CV
    create_configmap_yaml("jobsearch-cv", {"cv.txt": cv_text}, "02b-configmap-cv.yaml")
    
    # 3. Separar Configs y Secrets
    secret_keys = ["GOTIFY_TOKEN", "CF_CLIENT_ID", "CF_CLIENT_SECRET", "IA_BEARER_TOKEN"]
    exclude_keys = ["CRON_SCHEDULE_JOBSPY", "CRON_SCHEDULE_DISCOVERY"]
    
    configs = {k: v for k, v in env_vars.items() if k not in secret_keys and k not in exclude_keys}
    secrets = {k.lower().replace("_", "-"): v for k, v in env_vars.items() if k in secret_keys and v}

    create_configmap_yaml("jobsearch-config", configs, "02c-configmap-env.yaml")
    
    # 4. Manejo del Secreto
    plain_secret_file = "02d-secret-env-plain.yaml"
    sealed_secret_file = "06-secret-env-sealed.yaml"
    
    create_secret_plain_yaml("jobsearch-secrets", secrets, plain_secret_file)
    
    try:
        subprocess.run(["kubeseal", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        has_kubeseal = True
    except (subprocess.CalledProcessError, FileNotFoundError):
        has_kubeseal = False
        
    if has_kubeseal:
        print("[INFO] Sellando secreto con kubeseal...")
        kubeseal_cmd = ["kubeseal", "--format", "yaml"]
        
        if args.cert and os.path.exists(args.cert):
            print(f"[INFO] Usando certificado local ({args.cert}) para cifrado offline...")
            kubeseal_cmd.extend(["--cert", args.cert])
        elif args.cert:
            print(f"[WARN] El certificado {args.cert} no existe. Se intentara conexión al cluster.")

        with open(os.path.join(MANIFESTS_DIR, plain_secret_file), "r") as f_in:
            with open(os.path.join(MANIFESTS_DIR, sealed_secret_file), "w") as f_out:
                subprocess.run(
                    kubeseal_cmd,
                    stdin=f_in, stdout=f_out, check=True
                )
        os.remove(os.path.join(MANIFESTS_DIR, plain_secret_file))
        print(f"[OK] Secreto sellado creado en {sealed_secret_file}")
    else:
        print("[WARN] kubeseal no está instalado. Aplicando secreto plano temporalmente.")
        print("[WARN] No añadas 02d-secret-env-plain.yaml al repositorio.")

    # 5. Configurar schedules
    schedule_jobspy = env_vars.get("CRON_SCHEDULE_JOBSPY", "0 6 * * *")
    schedule_discovery = env_vars.get("CRON_SCHEDULE_DISCOVERY", "0 */3 * * *")
    
    update_cronjob_schedule("03-cronjob.yaml", schedule_jobspy)
    update_cronjob_schedule("03b-cronjob-discovery.yaml", schedule_discovery)
    
    # 6. Desplegar
    if args.prepare_only or args.prepare:
        print("[INFO] Modo prepare-only detectado. Manifiestos generados correctamente en la carpeta manifests/.")
        print("[INFO] Puedes hacer commit de estos cambios. Saltando kubectl apply.")
        return

    print("[INFO] Aplicando manifiestos con Kustomize...")
    result = subprocess.run(["kubectl", "apply", "-k", MANIFESTS_DIR], capture_output=True, text=True)
    if result.returncode == 0:
        print("[OK] Despliegue completado con éxito.")
    else:
        print("[ERROR] Falló kubectl apply:")
        print(result.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
