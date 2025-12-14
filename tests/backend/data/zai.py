from __future__ import annotations

from tests.backend.data import Chunk, JsonResponse, ResultData, Url

# Z.ai provider test data with thinking/reasoning tokens

SIMPLE_CONVERSATION_PARAMS: list[tuple[Url, JsonResponse, ResultData]] = [
    (
        "https://api.z.ai",
        {
            "id": "fake_id_1234",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "glm-4.6",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello! How can I help you today?",
                        "reasoning_content": "The user is asking for a greeting. I should respond politely.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "total_tokens": 150,
                "completion_tokens": 100,
            },
        },
        {
            "message": "Hello! How can I help you today?",
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": 50,
                "total_tokens": 150,
                "completion_tokens": 100,
            },
        },
    )
]

TOOL_CONVERSATION_PARAMS: list[tuple[Url, JsonResponse, ResultData]] = [
    (
        "https://api.z.ai",
        {
            "id": "fake_id_1234",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "glm-4.6",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "The user wants to know the weather in Beijing. I need to call the get_weather function.",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "Beijing", "unit": "celsius"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 80,
            },
        },
        {
            "message": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "name": "get_weather",
                    "arguments": '{"location": "Beijing", "unit": "celsius"}',
                    "index": 0,
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80},
        },
    )
]

STREAMED_SIMPLE_CONVERSATION_PARAMS: list[tuple[Url, list[Chunk], list[ResultData]]] = [
    (
        "https://api.z.ai",
        [
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"reasoning_content":"Thinking about"},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"reasoning_content":" the response..."},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"content":"Hello!"},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"content":" How can I help?"},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":50,"total_tokens":150,"completion_tokens":100}}',
            rb"data: [DONE]",
        ],
        [
            {
                "message": "",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "reasoning_content": "Thinking about",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "reasoning_content": " the response...",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "Hello!",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": " How can I help?",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 50, "completion_tokens": 100},
            },
        ],
    )
]


STREAMED_TOOL_CONVERSATION_PARAMS: list[tuple[Url, list[Chunk], list[ResultData]]] = [
    (
        "https://api.z.ai",
        [
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"reasoning_content":"I need to check the weather in Beijing."},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc123","type":"function","function":{"name":"get_weather"}}]},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":null,"type":"function","function":{"arguments":"{\"location\": \"Beijing\"}"}}]},"finish_reason":null}],"usage":null}',
            rb'data: {"id":"fake_id_1234","object":"chat.completion.chunk","created":1234567890,"model":"glm-4.6","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":100,"total_tokens":180,"completion_tokens":80}}',
            rb"data: [DONE]",
        ],
        [
            {
                "message": "",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "reasoning_content": "I need to check the weather in Beijing.",
                "finish_reason": None,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "finish_reason": None,
                "tool_calls": [{"name": "get_weather", "arguments": None, "index": 0}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "finish_reason": None,
                "tool_calls": [
                    {
                        "name": None,
                        "arguments": '{"location": "Beijing"}',
                        "index": 0,
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            {
                "message": "",
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            },
        ],
    )
]
