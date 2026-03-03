document.addEventListener('DOMContentLoaded', ()=>{
  const addBtn = document.getElementById('addBtn')
  if(addBtn){
    addBtn.addEventListener('click', async ()=>{
      const qty = parseInt(document.getElementById('qty').value||1)
      const res = await fetch('/cart/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({item_id: ITEM_ID, qty})})
      if(res.ok) alert('Added to cart')
    })
  }
  const orderNow = document.getElementById('orderNow')
  if(orderNow){
    orderNow.addEventListener('click', async ()=>{
      const qty = parseInt(document.getElementById('qty').value||1)
      await fetch('/cart/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({item_id: ITEM_ID, qty})})
      // redirect to checkout where user enters name/phone
      window.location = `/order/checkout?table=${encodeURIComponent(TABLE)}`
    })
  }
  const placeOrder = document.getElementById('placeOrder')
  if(placeOrder){
    placeOrder.addEventListener('click', async ()=>{
      const table = new URLSearchParams(location.search).get('table') || ''
      // go to checkout to collect name/phone
      window.location = `/order/checkout?table=${encodeURIComponent(table)}`
    })
  }
})
