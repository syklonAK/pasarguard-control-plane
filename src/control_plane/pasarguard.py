from dataclasses import dataclass
import httpx
@dataclass
class Client:
    base_url:str; api_key:str|None=None; owner_username:str|None=None; owner_password:str|None=None; verify_tls:bool=True
    def call(self,method,path,*,owner=False,json=None,data=None):
        headers={}
        if owner:headers["Authorization"]="Bearer "+self.owner_token()
        elif self.api_key:headers["X-Api-Key"]=self.api_key
        else:raise RuntimeError("missing credential")
        with httpx.Client(base_url=self.base_url.rstrip("/"),verify=self.verify_tls,timeout=20) as c:r=c.request(method,path,headers=headers,json=json,data=data)
        r.raise_for_status();return r.json() if r.content else {}
    def owner_token(self):
        with httpx.Client(base_url=self.base_url.rstrip("/"),verify=self.verify_tls,timeout=20) as c:r=c.post("/api/admin/token",data={"username":self.owner_username,"password":self.owner_password})
        r.raise_for_status();return r.json()["access_token"]
    def admins(self):return self.call("GET","/api/admins")
    def create_admin(self,payload):return self.call("POST","/api/admin",owner=True,json=payload)
    def modify_admin(self,admin_id,payload):return self.call("PUT",f"/api/admin/by-id/{admin_id}",owner=True,json=payload)
    def disable_admin_users(self,admin_id):return self.call("POST",f"/api/admin/by-id/{admin_id}/users/disable",owner=True)
    def nodes(self):return self.call("GET","/api/nodes")
    def reconnect_node(self,node_id):return self.call("POST",f"/api/node/{node_id}/reconnect")
