"""
SyncProgressTracker - High-performance progress tracking and display system for IMDBTraktSyncer.
Provides live progress bars, stats tracking, and optimized IMDB ID resolution with caching.
"""

import time
import sys
import threading
from datetime import datetime
from collections import defaultdict


class SyncProgressTracker:
    """
    Real-time progress tracker with animated display for long-running sync operations.
    Provides visual feedback with progress bars, ETA calculations, and stats.
    """
    
    # Progress bar characters for smooth animation
    PROGRESS_CHARS = ['░', '▒', '▓', '█']
    SPINNER_CHARS = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    
    def __init__(self, total_items=0, description="Processing", show_bar=True, bar_width=30):
        self.total_items = total_items
        self.processed_items = 0
        self.description = description
        self.show_bar = show_bar
        self.bar_width = bar_width
        self.start_time = None
        self.last_update_time = 0
        self.update_interval = 0.1  # Update display at most every 100ms
        self.spinner_idx = 0
        self.sub_tasks = {}
        self.stats = defaultdict(int)
        self._lock = threading.Lock()
        self._active = False
        
    def start(self, total_items=None, description=None):
        """Start or restart the progress tracker."""
        with self._lock:
            if total_items is not None:
                self.total_items = total_items
            if description is not None:
                self.description = description
            self.processed_items = 0
            self.start_time = time.time()
            self.last_update_time = 0
            self._active = True
            self._display_progress()
        return self
    
    def update(self, increment=1, status_text=None):
        """Update progress by increment amount."""
        with self._lock:
            self.processed_items += increment
            current_time = time.time()
            
            # Throttle display updates
            if current_time - self.last_update_time >= self.update_interval:
                self._display_progress(status_text)
                self.last_update_time = current_time
    
    def set_progress(self, current, total=None, status_text=None):
        """Set absolute progress values."""
        with self._lock:
            self.processed_items = current
            if total is not None:
                self.total_items = total
            self._display_progress(status_text)
            self.last_update_time = time.time()
    
    def add_stat(self, key, value=1):
        """Track a statistic."""
        with self._lock:
            self.stats[key] += value
    
    def finish(self, final_message=None):
        """Complete the progress tracker and show final stats."""
        with self._lock:
            self._active = False
            elapsed = time.time() - self.start_time if self.start_time else 0
            
            # Clear line and show completion
            sys.stdout.write('\r' + ' ' * 80 + '\r')
            
            if final_message:
                print(f"    ✓ {final_message}", flush=True)
            else:
                items_per_sec = self.processed_items / elapsed if elapsed > 0 else 0
                print(f"    ✓ {self.description} complete: {self.processed_items} items in {elapsed:.1f}s ({items_per_sec:.1f}/s)", flush=True)
            
            # Show stats if any
            if self.stats:
                stats_str = ', '.join(f"{k}: {v}" for k, v in self.stats.items())
                print(f"      Stats: {stats_str}", flush=True)
    
    def _display_progress(self, status_text=None):
        """Internal method to render progress display."""
        if not self._active:
            return
            
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        if self.total_items > 0:
            progress = self.processed_items / self.total_items
            percentage = progress * 100
            
            # Calculate ETA
            if self.processed_items > 0 and progress < 1:
                eta_seconds = (elapsed / progress) - elapsed
                eta_str = self._format_time(eta_seconds)
            else:
                eta_str = "--:--"
            
            if self.show_bar:
                # Build progress bar
                filled = int(self.bar_width * progress)
                bar = '█' * filled + '░' * (self.bar_width - filled)
                
                status = status_text if status_text else f"{self.processed_items}/{self.total_items}"
                line = f"\r    {self.description}: [{bar}] {percentage:5.1f}% | {status} | ETA: {eta_str}"
            else:
                status = status_text if status_text else f"{self.processed_items}/{self.total_items}"
                line = f"\r    {self.description}: {percentage:5.1f}% | {status} | ETA: {eta_str}"
        else:
            # Indeterminate progress with spinner
            self.spinner_idx = (self.spinner_idx + 1) % len(self.SPINNER_CHARS)
            spinner = self.SPINNER_CHARS[self.spinner_idx]
            status = status_text if status_text else f"{self.processed_items} items"
            line = f"\r    {spinner} {self.description}: {status} | {self._format_time(elapsed)}"
        
        # Pad to clear previous content
        sys.stdout.write(line.ljust(100))
        sys.stdout.flush()
    
    @staticmethod
    def _format_time(seconds):
        """Format seconds as mm:ss or hh:mm:ss."""
        if seconds < 0 or seconds > 86400:  # Sanity check
            return "--:--"
        if seconds >= 3600:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            return f"{h}:{m:02d}:{s:02d}"
        else:
            m = int(seconds // 60)
            s = int(seconds % 60)
            return f"{m}:{s:02d}"


class CachedIMDBResolver:
    """
    High-performance IMDB ID resolver with caching and lightweight HEAD requests.
    Resolves outdated/redirected IMDB IDs without loading full pages.
    """
    
    def __init__(self):
        self._cache = {}  # Maps original_id -> resolved_id
        self._pending = set()  # IDs that need resolution
        self.stats = {
            'cache_hits': 0,
            'resolved': 0,
            'errors': 0
        }
    
    def add_ids_to_resolve(self, imdb_ids):
        """Queue IDs for resolution."""
        for imdb_id in imdb_ids:
            if imdb_id and imdb_id not in self._cache:
                self._pending.add(imdb_id)
    
    def get_cached(self, imdb_id):
        """Get cached resolution if available."""
        if imdb_id in self._cache:
            self.stats['cache_hits'] += 1
            return self._cache[imdb_id]
        return None
    
    def resolve_batch_with_driver(self, driver, wait, progress_callback=None):
        """
        Resolve all pending IDs using the webdriver.
        Uses HEAD requests first, falls back to full page load only if needed.
        """
        import requests
        from IMDBTraktSyncer import errorHandling as EH
        
        pending_list = list(self._pending)
        total = len(pending_list)
        
        if total == 0:
            return
        
        for idx, imdb_id in enumerate(pending_list):
            if imdb_id in self._cache:
                continue
            
            if progress_callback:
                progress_callback(idx + 1, total, imdb_id)
            
            resolved_id = imdb_id  # Default to same if resolution fails
            
            try:
                # Try lightweight HEAD request first (faster, no page render)
                url = f"https://www.imdb.com/title/{imdb_id}/"
                try:
                    response = requests.head(url, allow_redirects=True, timeout=10,
                                             headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    if response.status_code == 200:
                        # Extract resolved ID from final URL
                        final_url = response.url
                        if '/title/' in final_url:
                            resolved_id = final_url.split('/title/')[1].split('/')[0]
                            self.stats['resolved'] += 1
                except (requests.RequestException, Exception):
                    # HEAD request failed, fall back to driver
                    try:
                        success, status_code, resolved_url, driver, wait = EH.get_page_with_retries(url, driver, wait, total_wait_time=30)
                        if success and '/title/' in resolved_url:
                            resolved_id = resolved_url.split('/title/')[1].split('/')[0]
                            self.stats['resolved'] += 1
                    except Exception:
                        self.stats['errors'] += 1
            except Exception:
                self.stats['errors'] += 1
            
            self._cache[imdb_id] = resolved_id
            self._pending.discard(imdb_id)
        
        return driver, wait
    
    def apply_resolutions(self, item_list, id_key='IMDB_ID'):
        """Apply cached resolutions to a list of items."""
        updated_count = 0
        for item in item_list:
            original_id = item.get(id_key)
            if original_id and original_id in self._cache:
                resolved_id = self._cache[original_id]
                if resolved_id != original_id:
                    item[id_key] = resolved_id
                    updated_count += 1
        return updated_count


def print_phase_header(phase_name, icon="📋"):
    """Print a styled phase header."""
    print(f"\n{icon} {phase_name}", flush=True)
    print(f"{'─' * (len(phase_name) + 3)}", flush=True)


def print_phase_complete(phase_name, elapsed_time=None, item_count=None):
    """Print a styled phase completion message."""
    parts = [f"✓ {phase_name} complete"]
    if elapsed_time is not None:
        parts.append(f"({elapsed_time:.1f}s)")
    if item_count is not None:
        parts.append(f"[{item_count} items]")
    print(f"  {' '.join(parts)}", flush=True)


def print_stats_summary(stats_dict, title="Summary"):
    """Print a formatted stats summary."""
    if not stats_dict:
        return
    print(f"\n  📊 {title}:", flush=True)
    for key, value in stats_dict.items():
        print(f"     • {key}: {value}", flush=True)


class DataAnalyzer:
    """
    Fast data comparison and analysis engine with progress tracking.
    Analyzes differences between Trakt and IMDB data sets.
    """
    
    def __init__(self, progress_tracker=None):
        self.progress = progress_tracker or SyncProgressTracker()
        self.results = {}
    
    def analyze_all(self, trakt_data, imdb_data, data_types):
        """
        Analyze multiple data types in one pass with progress tracking.
        
        Args:
            trakt_data: dict with keys like 'ratings', 'watchlist', etc.
            imdb_data: dict with matching keys
            data_types: list of data type names to analyze
        
        Returns:
            dict with analysis results for each type
        """
        total_ops = len(data_types) * 3  # filter, compare, sort for each type
        self.progress.start(total_ops, "Analyzing data")
        
        results = {}
        for dtype in data_types:
            trakt_list = trakt_data.get(dtype, [])
            imdb_list = imdb_data.get(dtype, [])
            
            self.progress.update(status_text=f"Filtering {dtype}")
            
            # Build sets for fast lookup
            trakt_ids = {item.get('IMDB_ID') for item in trakt_list if item.get('IMDB_ID')}
            imdb_ids = {item.get('IMDB_ID') for item in imdb_list if item.get('IMDB_ID')}
            
            # Find items to sync
            to_trakt = [item for item in imdb_list if item.get('IMDB_ID') not in trakt_ids]
            to_imdb = [item for item in trakt_list if item.get('IMDB_ID') not in imdb_ids]
            
            self.progress.update(status_text=f"Comparing {dtype}")
            
            results[dtype] = {
                'to_trakt': to_trakt,
                'to_imdb': to_imdb,
                'trakt_count': len(trakt_list),
                'imdb_count': len(imdb_list),
                'sync_to_trakt': len(to_trakt),
                'sync_to_imdb': len(to_imdb)
            }
            
            self.progress.update(status_text=f"Sorted {dtype}")
        
        self.progress.finish()
        return results

