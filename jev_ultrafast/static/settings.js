const token = document.querySelector('meta[name="demo-token"]').content;
const form = document.getElementById("provider-form");
const keyInput = document.getElementById("openrouter-key");
const saveButton = document.getElementById("save-key");
const status = document.getElementById("provider-status");

function showKeyStatus(source) {
  status.dataset.state = "";
  status.textContent = source === "encrypted"
    ? "Chave salva com proteção do Windows."
    : source === "environment"
      ? "Chave configurada no ambiente."
      : "Nenhuma chave configurada.";
}

async function loadStatus() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) throw Error("Não foi possível consultar a configuração.");
  const state = await response.json();
  showKeyStatus(state.openrouter_key_source);
}

async function encryptKey(key) {
  const response = await fetch("/api/public-key", { cache: "no-store" });
  if (!response.ok) throw Error("Não foi possível preparar o envio seguro da chave.");
  const spki = Uint8Array.from(atob((await response.json()).public_key), (char) => char.charCodeAt(0));
  const publicKey = await crypto.subtle.importKey("spki", spki, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
  const cipher = await crypto.subtle.encrypt("RSA-OAEP", publicKey, new TextEncoder().encode(key));
  return btoa(String.fromCharCode(...new Uint8Array(cipher)));
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = keyInput.value.trim();
  if (!key || saveButton.disabled) return;
  keyInput.disabled = true;
  saveButton.disabled = true;
  status.dataset.state = "";
  status.textContent = "Validando e salvando a chave…";
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Demo-Token": token },
      body: JSON.stringify({ encrypted_key: await encryptKey(key) }),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Não foi possível salvar a chave.");
    keyInput.value = "";
    showKeyStatus(result.openrouter_key_source);
  } catch (error) {
    status.dataset.state = "error";
    status.textContent = error.message || "Não foi possível salvar a chave.";
  } finally {
    keyInput.disabled = false;
    saveButton.disabled = false;
  }
});

loadStatus().catch((error) => {
  status.dataset.state = "error";
  status.textContent = error.message || "Não foi possível consultar a configuração.";
});
