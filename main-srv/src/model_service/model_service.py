"""
main-srv/src/model_service/model_service.py

Сервис взаимодействия с LLM-сервером (llama-server) через OpenAI API.

ВХОД (все параметры обязательные, без дефолтов):
    - messages: List[Dict[role, content]]
    - temperature: float
    - top_p: float
    - top_k: int
    - min_p: float
    - max_tokens: int
    - presence_penalty: float
    - stop: List[str]

ВЫХОД:
    {
        "success": bool,
        "response": str,           # чистый ответ (content)
        "reasoning": str,          # рассуждение (reasoning_content), может быть ""
        "metrics": {
            "usage": {...},        # prompt_tokens, completion_tokens, total_tokens
            "timings": {...},      # prompt_ms, predicted_per_second, etc.
            "model": str,          # имя модели из ответа
            "id": str,             # ID запроса
            "host_nctx": int       # n_ctx из конфига
        },
        "error": str               # пусто при успехе
    }

НЕ ДЕЛАЕТ:
    - Не пишет в БД
    - Не считает latency (задача вызывающего модуля)
    - Не логирует тела запросов/ответов
    - Не принимает orchestrator_step_id, prompt_id, kaya_version
"""

__version__ = "1.0.0"
__description__ = "HTTP-клиент для llama-server с FIFO-очередью"

import json
import yaml
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from threading import Lock
from queue import Queue, Full
import httpx

logger = logging.getLogger(__name__)


class ModelService:
    """
    Singleton-сервис для вызовов llama-server.
    """
    
    _instance: Optional["ModelService"] = None
    _init_lock = Lock()
    
    def __new__(cls, config_path: Optional[str] = None):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config_path: Optional[str] = None):
        if self._initialized:
            return
        self._initialized = True
        
        self.config = self._load_config(config_path)
        self.base_url = self._build_url()
        self.timeout = self.config["llm_server"]["timeout"]
        self.model_name = self.config["model"]["name"]
        self.host_nctx = self.config["model"]["n_ctx"]
        
        self.client = httpx.Client(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self.timeout,
                write=30.0,
                pool=60.0
            ),
            headers={"Content-Type": "application/json"}
        )
        
        self._queue_enabled = self.config["queue"]["enabled"]
        self._queue: Queue = Queue(maxsize=self.config["queue"]["max_size"])
        self._lock = Lock()
        
        logger.info("ModelService инициализирован: %s, модель: %s, n_ctx: %d", 
                   self.base_url, self.model_name, self.host_nctx)
    
    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        if config_path is None:
            config_path = Path(__file__).parent.parent.parent / "configs" / "model_config.yaml"
        else:
            config_path = Path(config_path)
        
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    
    def _build_url(self) -> str:
        cfg = self.config["llm_server"]
        return f"{cfg['protocol']}://{cfg['host']}:{cfg['port']}{cfg['endpoint']}"
    
    def _call_server(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """HTTP-вызов к llama-server с повторными попытками."""
        retry = self.config["retry"]
        last_error = None
        
        for attempt in range(1, retry["max_attempts"] + 1):
            try:
                response = self.client.post(
                    url=self.base_url,
                    json=payload,
                    timeout=self.timeout
                )
                response.raise_for_status()
                return {"success": True, "data": response.json(), "error": ""}
                
            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                logger.warning("Попытка %d/%d: %s", attempt, retry["max_attempts"], last_error)
                
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    last_error = f"Server {e.response.status_code}: {e}"
                    logger.warning("Попытка %d/%d: %s", attempt, retry["max_attempts"], last_error)
                else:
                    return {"success": False, "data": None, "error": f"HTTP {e.response.status_code}: {e.response.text}"}
                    
            except httpx.RequestError as e:
                last_error = f"Network: {e}"
                logger.warning("Попытка %d/%d: %s", attempt, retry["max_attempts"], last_error)
                
            except Exception as e:
                last_error = f"Unexpected: {e}"
                logger.error("Ошибка: %s", last_error, exc_info=True)
                break
            
            if attempt < retry["max_attempts"]:
                time.sleep(retry["backoff_seconds"] * (2 ** (attempt - 1)))
        
        return {"success": False, "data": None, "error": last_error}
    
    def _parse_response(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Парсит ответ llama-server и извлекает content + reasoning_content.
    
        Формат ответа llama-server:
        {
        "choices": [{
            "message": {
            "content": "...",
            "reasoning_content": "..."
            }
        }],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 280,
            "total_tokens": 300
        },
        "timings": {
            "cache_n": 0,              
            "prompt_n": 20,            
            "prompt_ms": 56.113,
            "prompt_per_token_ms": 2.80565,
            "prompt_per_second": 356.42,
            "predicted_n": 280,        
            "predicted_ms": 3793.907,
            "predicted_per_token_ms": 13.549,
            "predicted_per_second": 73.80
        }
        }
        """
        try:
            message = raw_data["choices"][0]["message"]
            content = message.get("content", "")
            reasoning = message.get("reasoning_content", "")
            usage = raw_data.get("usage", {})
            timings = raw_data.get("timings", {})
            
            return {
                "success": True,
                "response": content,
                "reasoning": reasoning,
                "metrics": {
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0)
                    },
                    "timings": {
                        "cache_n": timings.get("cache_n", 0),              
                        "prompt_n": timings.get("prompt_n", 0),            
                        "prompt_ms": timings.get("prompt_ms", 0.0),
                        "prompt_per_token_ms": timings.get("prompt_per_token_ms", 0.0),
                        "prompt_per_second": timings.get("prompt_per_second", 0.0),
                        "predicted_n": timings.get("predicted_n", 0),      
                        "predicted_ms": timings.get("predicted_ms", 0.0),
                        "predicted_per_token_ms": timings.get("predicted_per_token_ms", 0.0),
                        "predicted_per_second": timings.get("predicted_per_second", 0.0)
                    },
                    "model": raw_data.get("model", ""),
                    "id": raw_data.get("id", ""),
                    "host_nctx": self.host_nctx
                },
                "error": ""
            }
        except (KeyError, IndexError, TypeError) as e:
            return {
                "success": False,
                "response": "",
                "reasoning": "",
                "metrics": {},
                "error": f"Parse: {e}"
            }
        
    def _process_with_queue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Обработка через FIFO-очередь."""
        try:
            self._queue.put_nowait(payload)
        except Full:
            logger.warning("Очередь запросов переполнена")
            return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": "Queue full"}
        
        with self._lock:
            while not self._queue.empty():
                item = self._queue.get_nowait()
                result = self._call_server(item)
                self._queue.task_done()
                
                if not result["success"]:
                    return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": result["error"]}
                
                return self._parse_response(result["data"])
        
        return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": "Queue processing failed"}
    
    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        top_p: float,
        top_k: int,
        min_p: float,
        max_tokens: int,
        presence_penalty: float,
        stop: List[str]
    ) -> Dict[str, Any]:
        """
        Генерация ответа от модели.
        
        ВСЕ ПАРАМЕТРЫ ОБЯЗАТЕЛЬНЫЕ. Нет дефолтных значений.
        """
        if not isinstance(messages, list) or len(messages) == 0:
            return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": "messages: требуется непустой список"}
        
        if not isinstance(stop, list):
            return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": "stop: требуется список строк"}
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "min_p": min_p,
            "max_tokens": max_tokens,
            "presence_penalty": presence_penalty,
            "stop": stop,
            "stream": False
        }
        
        if self._queue_enabled:
            return self._process_with_queue(payload)
        else:
            result = self._call_server(payload)
            if not result["success"]:
                return {"success": False, "response": "", "reasoning": "", "metrics": {}, "error": result["error"]}
            
            return self._parse_response(result["data"])
    
    def is_busy(self) -> bool:
        """Проверка: есть ли запросы в очереди."""
        return self._queue.qsize() > 0 if self._queue_enabled else False
    
    def close(self):
        """Закрытие HTTP-соединений."""
        if hasattr(self, "client"):
            self.client.close()
            logger.debug("HTTP-соединения закрыты")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False