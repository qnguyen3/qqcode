# Images

QQcode can read image files and send them to vision-capable models.

## Reading images

`read_file` detects image extensions (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`,
`.bmp`) and returns a reference to the image instead of its bytes:

- The tool result describes the file (media type, size, dimensions) and is
  shown in the tool output like any other read.
- When the active model has `capabilities.vision = true`, QQcode attaches the
  image to the conversation so the model can see it.
- When the model has no vision support, the image is replaced by a text
  placeholder (`[image image/png, 1234 bytes from /path/to/file.png]`) and the
  turn keeps working.

Images are attached to a user message after the tool result, so every provider
gets them in a format it accepts.

## Limits

| Limit | Value |
| --- | --- |
| Max file size | 5 MiB (`MAX_IMAGE_BYTES`) |
| Max long edge | 1568 px (`MAX_IMAGE_DIMENSION`) |

Files over the size limit are resized when [Pillow](https://python-pillow.org)
is installed:

```bash
pip install "qqcode[images]"
```

Without Pillow, oversized images are refused with a clear message and the tool
call fails; everything else keeps working.

## Sessions

Session logs store image **references** (path, media type, size), never the
base64 payload, so reading many images does not bloat `.jsonl` files. On
replay, the file is re-read when it still exists; if it was deleted, the image
degrades to the same placeholder text.

## Provider encoding

| API style | Encoding |
| --- | --- |
| OpenAI-compatible (`openai`) | `{"type": "image_url", "image_url": {"url": "data:<media>;base64,..."}}` |
| Anthropic messages | `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}` |

Mistral backends currently downgrade images to placeholder text. Base64 is
loaded lazily at request time and cached per file (path, mtime, size).
