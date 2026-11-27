/**
 * Client-side dynamic interaction and live refresh for YourIpoBot.
 */

async function triggerPipeline() {
    const btn = document.querySelector('.btn-primary');
    const originalText = btn.innerHTML;
    
    btn.innerHTML = '<span class="btn-icon">⏳</span> Computing Real-Time Updates...';
    btn.disabled = true;

    try {
        const response = await fetch('/api/run_pipeline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const res = await response.json();
        if (response.ok) {
            btn.innerHTML = '<span class="btn-icon">✅</span> Updated!';
            setTimeout(() => {
                window.location.reload();
            }, 1000);
        } else {
            alert('Pipeline error: ' + (res.message || 'Unknown error'));
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    } catch (err) {
        console.error('Trigger error:', err);
        alert('Network or server connection failed.');
        btn.innerHTML = originalText;
        btn.disabled = false;
    }
}

/**
 * Filter IPO cards by board: 'ALL', 'MAINBOARD', or 'SME'.
 */
function filterBoard(type, btn) {
    const filterBtns = document.querySelectorAll('.filter-btn');
    filterBtns.forEach(b => b.classList.remove('active'));
    if (btn) {
        btn.classList.add('active');
    }

    const cards = document.querySelectorAll('.ipo-card');
    cards.forEach(card => {
        const board = (card.getAttribute('data-board') || '').toUpperCase();
        if (type === 'ALL' || board === type) {
            card.style.display = 'block';
        } else {
            card.style.display = 'none';
        }
    });
}
