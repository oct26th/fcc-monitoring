"""Circuit Breaker pattern implementation for crawler fault tolerance."""
import logging
import time
from enum import Enum
from typing import Callable, Any, Optional
from dataclasses import dataclass, field

from loguru import logger

logger = logging.getLogger("fcc_monitor.circuit_breaker")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"         # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5       # Failures before opening circuit
    success_threshold: int = 2       # Successes in half-open before closing
    timeout: float = 60.0           # Seconds before trying half-open
    excluded_exceptions: tuple = ()  # Exceptions that don't count as failures


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: Optional[float] = None
    state: CircuitState = CircuitState.CLOSED
    state_history: list = field(default_factory=list)


class CircuitBreaker:
    """
    Circuit breaker implementation for preventing cascade failures.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests are rejected immediately
    - HALF_OPEN: Testing if service recovered, limited requests allowed
    """
    
    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.stats = CircuitBreakerStats()
        self._lock = None  # Will use simple threading lock if needed
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function through circuit breaker.
        
        Args:
            func: Function to call
            *args, **kwargs: Arguments to pass to function
            
        Returns:
            Result from function
            
        Raises:
            CircuitBreakerOpen: If circuit is open
            Exception: Any exception from function (if not excluded)
        """
        self._check_and_update_state()
        
        if self.stats.state == CircuitState.OPEN:
            raise CircuitBreakerOpen(f"Circuit {self.name} is OPEN")
        
        self.stats.total_calls += 1
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            # Check if this exception type should be excluded
            if self._is_excluded_exception(e):
                logger.debug(f"Circuit {self.name}: Exception {type(e).__name__} excluded from failure count")
                raise
            self._on_failure()
            raise
    
    def _is_excluded_exception(self, exception: Exception) -> bool:
        """Check if exception type is excluded from failure counting."""
        return isinstance(exception, self.config.excluded_exceptions)
    
    def _check_and_update_state(self):
        """Check timeout and update circuit state accordingly."""
        current_time = time.time()
        
        if self.stats.state == CircuitState.OPEN:
            # Check if timeout has passed to try half-open
            if self.stats.last_failure_time:
                if current_time - self.stats.last_failure_time >= self.config.timeout:
                    logger.info(f"Circuit {self.name}: OPEN -> HALF_OPEN (timeout elapsed)")
                    self._record_state_change(CircuitState.HALF_OPEN)
                    self.stats.state = CircuitState.HALF_OPEN
                    self.stats.consecutive_successes = 0
    
    def _on_success(self):
        """Handle successful call."""
        self.stats.successful_calls += 1
        self.stats.consecutive_successes += 1
        self.stats.consecutive_failures = 0
        
        if self.stats.state == CircuitState.HALF_OPEN:
            if self.stats.consecutive_successes >= self.config.success_threshold:
                logger.info(f"Circuit {self.name}: HALF_OPEN -> CLOSED (recovered)")
                self._record_state_change(CircuitState.CLOSED)
                self.stats.state = CircuitState.CLOSED
    
    def _on_failure(self):
        """Handle failed call."""
        self.stats.failed_calls += 1
        self.stats.consecutive_failures += 1
        self.stats.consecutive_successes = 0
        self.stats.last_failure_time = time.time()
        
        if self.stats.state == CircuitState.HALF_OPEN:
            # Failed during recovery test, go back to open
            logger.warning(f"Circuit {self.name}: HALF_OPEN -> OPEN (recovery failed)")
            self._record_state_change(CircuitState.OPEN)
            self.stats.state = CircuitState.OPEN
        elif self.stats.state == CircuitState.CLOSED:
            if self.stats.consecutive_failures >= self.config.failure_threshold:
                logger.warning(f"Circuit {self.name}: CLOSED -> OPEN (threshold reached)")
                self._record_state_change(CircuitState.OPEN)
                self.stats.state = CircuitState.OPEN
    
    def _record_state_change(self, new_state: CircuitState):
        """Record state change in history."""
        self.stats.state_history.append({
            "timestamp": time.time(),
            "from": self.stats.state.value,
            "to": new_state.value
        })
        # Keep only last 20 state changes
        if len(self.stats.state_history) > 20:
            self.stats.state_history = self.stats.state_history[-20:]
    
    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "name": self.name,
            "state": self.stats.state.value,
            "total_calls": self.stats.total_calls,
            "successful_calls": self.stats.successful_calls,
            "failed_calls": self.stats.failed_calls,
            "consecutive_failures": self.stats.consecutive_failures,
            "last_failure_time": self.stats.last_failure_time
        }
    
    def reset(self):
        """Manually reset circuit breaker to closed state."""
        logger.info(f"Circuit {self.name}: Manual reset to CLOSED")
        self.stats = CircuitBreakerStats()
        self.stats.state = CircuitState.CLOSED


class CircuitBreakerOpen(Exception):
    """Exception raised when circuit breaker is open."""
    pass


# Global circuit breaker registry
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, **config_kwargs) -> CircuitBreaker:
    """
    Get or create a circuit breaker by name.
    
    Args:
        name: Unique name for the circuit breaker
        **config_kwargs: Configuration options
        
    Returns:
        CircuitBreaker instance
    """
    if name not in _circuit_breakers:
        config = CircuitBreakerConfig(**config_kwargs)
        _circuit_breakers[name] = CircuitBreaker(name, config)
        logger.info(f"Created circuit breaker: {name}")
    return _circuit_breakers[name]


def get_all_circuit_breakers() -> dict[str, CircuitBreaker]:
    """Get all circuit breakers."""
    return _circuit_breakers


def reset_all_circuit_breakers():
    """Reset all circuit breakers (for testing)."""
    for cb in _circuit_breakers.values():
        cb.reset()
    logger.info("All circuit breakers reset")
