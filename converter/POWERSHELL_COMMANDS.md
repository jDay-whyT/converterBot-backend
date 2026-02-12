# PowerShell команды для тестирования конвертера

## 1. Запустить сервер конвертера

```powershell
# Перейти в папку converter
cd converter

# Установить зависимости (если не установлены)
pip install -r requirements.txt

# Установить переменные окружения
$env:CONVERTER_API_KEY = "test_api_key_123"
$env:MAX_FILE_MB = "50"

# Запустить сервер
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## 2. Проверить работу сервера (в новом окне PowerShell)

```powershell
# Проверить health endpoint
Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get
```

## 3. Конвертировать одно фото

```powershell
# Переменные
$API_KEY = "test_api_key_123"
$headers = @{ "X-API-KEY" = $API_KEY }

# Конвертировать DNG файл
$form = @{
    file = Get-Item -Path "..\IMG_1211.DNG"
    quality = "85"
}

Invoke-RestMethod -Uri "http://localhost:8000/convert" `
    -Method Post `
    -Form $form `
    -Headers $headers `
    -OutFile "output_IMG_1211.jpg"

Write-Host "✓ Конвертировано: output_IMG_1211.jpg" -ForegroundColor Green
```

## 4. Конвертировать все тестовые файлы

```powershell
# Создать папку для результатов
New-Item -ItemType Directory -Path "converted" -Force

$API_KEY = "test_api_key_123"
$headers = @{ "X-API-KEY" = $API_KEY }

# Список файлов для конвертации
$files = @(
    "..\IMG_1211.DNG",
    "..\IMG_2557.HEIF",
    "..\IMG_3837.CR3",
    "..\IMG_5254.DNG"
)

foreach ($file in $files) {
    if (Test-Path $file) {
        $fileName = [System.IO.Path]::GetFileNameWithoutExtension($file)
        Write-Host "`nКонвертирую: $file" -ForegroundColor Yellow

        $form = @{
            file = Get-Item -Path $file
            quality = "85"
        }

        try {
            Invoke-RestMethod -Uri "http://localhost:8000/convert" `
                -Method Post `
                -Form $form `
                -Headers $headers `
                -OutFile "converted\$fileName.jpg"

            Write-Host "✓ Сохранено: converted\$fileName.jpg" -ForegroundColor Green
        } catch {
            Write-Host "✗ Ошибка: $($_.Exception.Message)" -ForegroundColor Red
        }
    } else {
        Write-Host "Файл не найден: $file" -ForegroundColor Red
    }
}
```

## 5. Конвертировать с изменением размера

```powershell
$API_KEY = "test_api_key_123"
$headers = @{ "X-API-KEY" = $API_KEY }

# Конвертировать с максимальной стороной 2048px
$form = @{
    file = Get-Item -Path "..\IMG_1211.DNG"
    quality = "90"
    max_side = "2048"
}

Invoke-RestMethod -Uri "http://localhost:8000/convert" `
    -Method Post `
    -Form $form `
    -Headers $headers `
    -OutFile "output_resized.jpg"

Write-Host "✓ Конвертировано с resize: output_resized.jpg" -ForegroundColor Green
```

## 6. Использовать готовый скрипт

```powershell
# Запустить готовый скрипт тестирования
.\test_converter.ps1
```

## Альтернатива: использовать curl (если установлен)

```powershell
# Конвертировать через curl
curl -X POST "http://localhost:8000/convert" `
    -H "X-API-KEY: test_api_key_123" `
    -F "file=@..\IMG_1211.DNG" `
    -F "quality=85" `
    --output "output_curl.jpg"
```

## Troubleshooting

### Если сервер не запускается:
```powershell
# Проверить, что порт 8000 свободен
netstat -ano | findstr :8000

# Убить процесс, если порт занят
Stop-Process -Id <PID> -Force
```

### Если ошибка "API key is not configured":
```powershell
# Установить переменную окружения
$env:CONVERTER_API_KEY = "test_api_key_123"
```

### Если ошибка "command not found" (exiftool, magick и т.д.):
- Эти инструменты должны быть установлены в системе
- Или запустите через Docker (см. ниже)

## Запуск через Docker (рекомендуется)

```powershell
# Собрать Docker образ
docker build -t converter-service .

# Запустить контейнер
docker run -d -p 8000:8000 `
    -e CONVERTER_API_KEY=test_api_key_123 `
    -e MAX_FILE_MB=50 `
    --name converter `
    converter-service

# Проверить логи
docker logs converter -f
```

После этого используйте команды выше для отправки файлов на `http://localhost:8000/convert`
