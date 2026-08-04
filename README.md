# Monitor de cupos para vacunación contra meningococo — MSP Uruguay

Revisa las cuatro agendas regionales oficiales del MSP y envía un mensaje por
Telegram cuando detecta turnos nuevos.

## Fuentes monitoreadas

- Región Sur
- Región Este
- Región Oeste
- Región Norte
- Página informativa oficial del MSP

La agenda temporal está dirigida actualmente a niñas, niños y adolescentes de
9 a 15 años. El monitor no agenda automáticamente: solamente avisa.

## Configuración en GitHub

### 1. Subir los archivos

Descomprimí el ZIP y subí **el contenido de la carpeta**, conservando la carpeta
oculta `.github`.

### 2. Crear los secretos

En el repositorio:

1. `Settings`
2. `Secrets and variables`
3. `Actions`
4. `New repository secret`

Creá:

- `TELEGRAM_BOT_TOKEN`: token entregado por BotFather.
- `TELEGRAM_CHAT_ID`: identificador numérico del chat de Telegram.

Nunca pongas el token dentro del código ni en un archivo público.

### 3. Habilitar permisos de escritura

En:

1. `Settings`
2. `Actions`
3. `General`
4. `Workflow permissions`

Elegí **Read and write permissions** y guardá.

Ese permiso permite actualizar `state.json` para no repetir el mismo aviso.

### 4. Probar Telegram

1. Abrí `Actions`.
2. Elegí `Monitor cupos MSP`.
3. `Run workflow`.
4. Marcá `Enviar únicamente un mensaje de prueba por Telegram`.
5. Ejecutá.

Debe llegar el mensaje: “Prueba correcta”.

### 5. Ejecutar una revisión real

Volvé a `Run workflow`, dejá la casilla de prueba sin marcar y ejecutá.

Luego el proceso corre automáticamente cada cinco minutos. GitHub puede demorar
la ejecución en momentos de alta carga; no es un sistema de tiempo real exacto.

## Qué hace el monitor

- Abre las agendas con Chromium mediante Playwright.
- Recorre los vacunatorios detectables.
- Extrae fechas y horarios visibles.
- Envía por Telegram solo los turnos que no estaban en `state.json`.
- Guarda capturas como artefacto si falla la navegación.

## Limitaciones

El SAE puede cambiar su estructura sin aviso. Si GitHub muestra ejecuciones
fallidas, abrí la ejecución y descargá `capturas-error`. El código usa varios
métodos de respaldo, pero no puede garantizar disponibilidad continua de un
sitio externo.

Instagram y X no se incluyen en esta primera versión porque bloquean o limitan
el scraping automatizado y producirían un monitor menos confiable. La fuente
principal y más rápida es la agenda oficial del SAE.
