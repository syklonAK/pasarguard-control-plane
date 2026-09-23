import unittest
from control_plane.domain import GIB,cascade,price_usage
class T(unittest.TestCase):
 def test_price(self):self.assertEqual(price_usage(GIB,5000),5000)
 def test_half(self):self.assertEqual(price_usage(GIB//2,5001),2501)
 def test_cascade(self):self.assertEqual([x.amount_irr for x in cascade(GIB,[("b","a",5000),("a","root",4000)])],[5000,4000])
if __name__=="__main__":unittest.main()
