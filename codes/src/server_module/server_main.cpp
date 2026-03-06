#include "../../include/httplib.h"
#include "../../include/json.hpp"
#include "../common/common.h"

#include <iostream>
#include <map>
#include <vector>
#include <mutex>
#include <algorithm>
using namespace std;

using json=nlohmann::json;

map<string,vector<MintedCoin>> inventory;
map<string,vector<GhostPacket>> mailbox;
mutex db_mutex;
int main()
{
	httplib::Server svr;
	//uploading keys
	svr.Post("/upload_keys",[](const httplib::Request& req, httplib::Response& res)
	{
		try
		{
			auto j=json::parse(req.body);
			lock_guard<mutex> guard(db_mutex);
			for(const auto& item:j)
			{
				MintedCoin coin=MintedCoin::from_json(item);
				inventory[coin.user_id].push_back(coin);
			}
			cout<<"[SERVER] Inventory Updated."<<endl;
			res.set_content("OK","text/plain");			
		}
		catch(...)
		{
			res.status=400;
			res.set_content("Invalid JSON","text/plain");
		}
	});
	
	//fetching keys
	svr.Get("/fetch_key", [](const httplib::Request& req, httplib::Response& res) 
	{
        	string user = req.get_param_value("user");
        	int tier_int = std::stoi(req.get_param_value("tier"));
        	Coin requested_tier = static_cast<Coin>(tier_int);

        	// CRITICAL SECTION: Lock Database
        	lock_guard<mutex> lock(db_mutex);

        	if (inventory.find(user) == inventory.end()) 
        	{
            		res.status = 404;
            		return;
        	}

        	auto& coins = inventory[user];
        
                auto it = find_if(coins.begin(), coins.end(), [requested_tier](const MintedCoin& c) { return c.coin == requested_tier; });

       		if (it != coins.end()) 
       		{
            		res.set_content(it->to_json().dump(), "application/json");        
            	        //coins.erase(it);
            		cout << "[Server] Dispensed Tier " << requested_tier << " key for " << user << endl;
            	}
                else 
                {
            		res.status = 404; 	
            		cout << "[Server] Warning: " << user << " is out of Tier " << requested_tier << " keys!" << endl;
        	}
    	});
	
	svr.Post("/send_msg",[](const httplib::Request& req, httplib::Response& res)
	{
		auto j=json::parse(req.body);
		GhostPacket pkt=GhostPacket::from_json(j);
		lock_guard<mutex> guard(db_mutex);
		mailbox[pkt.recipient_id].push_back(pkt);
		cout<<"[SERVER] Routed packet to "<<pkt.recipient_id<<endl;
		res.set_content("Sent","text/plain");
	});
	
	svr.Get("/check_mail",[](const httplib::Request& req, httplib:: Response& res)
	{
		string user=req.get_param_value("user");
		lock_guard<mutex>lock(db_mutex);
		json response=json::array();
		if(mailbox.count(user))
		{
			auto& inbox=mailbox[user];
			for(const auto& pkt:inbox)
			{
				response.push_back(pkt.to_json());
			}
			inbox.clear();
			cout<<"[SERVER] delivered "<<response.size()<<" messages to "<<user<<endl;
			
		}
		res.set_content(response.dump(),"application/json");		
	});
	
	cout<<"AQM Blind Courier running on: 8080"<<endl;
	cout<<"Waiting for clients"<<endl;
	svr.listen("0.0.0.0",8080);
	return 0;
}
